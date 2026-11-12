# Instant Delivery & SNS Studio (v4.1) 仕様・構造ドキュメント (SPEC.md)

## 1. アプリの目的と主な機能
イベント・ライブ撮影等におけるクライアント即納用データおよびSNS公開用データを、1回の読み込み（1パス）で複数パターン同時に並列生成するデスクトップ画像処理ツール。

### 主な機能
- **複数パターンの1パス並列一括書き出し**: 元比率（WMなし/あり）、SNS用比率（1:1 / 4:5 / 9:16）など複数の出力ルールをリスト管理し、`ThreadPoolExecutor` で並列JPEG出力（4:4:4 `subsampling=0`、ICCプロファイル維持）。
- **枠・背景ブラー生成エンジン**: 白枠・黒枠・四方余白調整に加え、元画像の四隅10%を自動クロップして周辺減光を除去した「背景ブラー（明・すりガラス / 標準 / 暗・シネマ風）」および明るさ・ぼかし量のスライダー微調整に対応。
- **マルチスロット・ウォーターマーク合成**: 最大4枠まで同時配置可能。透過PNG（余白自動トリム・明背景時の暗色反転対応）およびテキスト（PC内フォント検索・ローカル `fonts` フォルダ参照・文字間隔トラッキング調整）に対応し、文字/ロゴ本体の端を基準とした余白計算とドロップシャドウ付与を行う。
- **枠外EXIFテキスト印字**: カメラ名・レンズ名・露出情報（焦点距離 / F値 / SS / ISO / 撮影日）・自由追記テキスト（フィルム名等）を「センター2段」または「左右スプリット」で下部余白に印字。メーカー表記等の文字列置換ルールにも対応。
- **EXIFフィルタリング再構築 ＆ 複数カメラ時刻ズレ補正**: 読み込みフォルダ内のカメラ機種を自動検出し、分・秒単位の時刻オフセットを加算してEXIF撮影日時順（ミリ秒 `SubSecTimeOriginal` 対応）で連番リネーム・ソート。EXIF保存は4段階（①納品最適[日時のみ] / ②カメラ維持 / ③完全維持 / ④全削除）＋Copyright埋め込み・OSファイル更新日時同期（`os.utime`）に対応。
- **3モード切替リアルタイム・プレビュー**: 「全パターン比較（2×2グリッド）」「縦横向き比較（片方向のみ読込時は中央クロップで逆向きを仮想生成）」「1枚拡大」を50ms前後のデバウンス制御でリアルタイム描画。
- **設定管理・オートセーブ**: JSON形式でのプリセット保存・読込に加え、ウィンドウ終了時および書き出し実行時に `autosave_config.json` へ全UIステートを自動保存し、次回起動時に自動復元する。
- **フォルダ別ZIP自動生成**: 書き出し完了後、各出力パターンフォルダを個別にZIPアーカイブ化。

---

## 2. 動作環境と使用フレームワーク・ライブラリ
- **言語・想定ランタイム**: Python 3.12（Windows 10/11 実機稼働確認済、macOS クロスプラットフォーム対応コード実装済）
- **GUIフレームワーク**: `customtkinter` + 標準 `tkinter`（`Listbox` 等の一部ネイティブ部品）
- **ドラッグ＆ドロップ拡張**: `tkinterdnd2`（未導入環境では自動でボタン選択UIへフォールバック）
- **画像処理**: `Pillow` (`PIL`)
- **EXIF解析・バイナリ生成**: `piexif`
- **パッケージング / CI**: `PyInstaller`（Windows単一 `.exe` 化確認済、GitHub Actionsによる自動ビルド構成あり）

---

## 3. アーキテクチャ（主要クラス・関数と役割の切り分け）
現在は単一ファイル `app.py`（約1,100行）内に、以下の4層を分離して実装している。

### A. OS・パス解決・ユーティリティ層
- `BASE_APP_DIR` / `LOCAL_FONTS_DIR` / `AUTOSAVE_FILE`: スクリプト実行時とPyInstaller凍結時（`sys.frozen`、macOS `.app` バンドル階層含む）の双方で実行ファイル横のパスを解決。
- `open_folder(path_str)`: Windows (`os.startfile`) / macOS (`open`) / Linux (`xdg-open`) 対応のフォルダ展開関数。
- `scan_pc_fonts(force_refresh)` / `load_font(font_path, size)`: OS別のシステム・ユーザーフォントフォルダと `./fonts` をスキャンして `_SYSTEM_FONTS_CACHE` に保持し、指定フォントまたはOS標準フォントへ安全にフォールバックする。

### B. 画像処理・レンダリング層（ステートレス関数群）
- `render_single_pattern(base_img, meta, pattern, wm_configs, caption_cfg, preview_max_edge)`: 1枚の画像に対する単一パターンの統合レンダリング関数（キャンバス比率計算、リサイズ、枠・ブラー合成、WM配置、枠外EXIF文字描画）。プレビュー時は `preview_max_edge` 指定で縮小高速描画を行う。
- `generate_blurred_background(...)`: 中央80%クロップ＋`GaussianBlur`＋モード別色調補正（`ImageEnhance.Color` / 明暗レイヤー `Image.blend`）による背景ブラー生成。
- `create_text_watermark(...)` / `apply_drop_shadow(...)` / `paste_watermarks_on_surface(...)`: トラッキング付きテキスト画像生成、短辺基準のドロップシャドウ生成、およびシャドウの透明パディング量を差し引いた四隅への正確なアルファ合成。
- `place_on_fixed_stage(...)` / `make_simulated_opposite_orientation(...)` / `create_sample_dummy_image(...)`: プレビュー時のアスペクト比変化によるUI揺れを防ぐ固定台紙配置、仮想縦横クロップ生成、未読込時のダミー画像生成。

### C. EXIFメタデータ処理層
- `extract_exif_metadata(img_path)`: `piexif.load` で日時・ミリ秒・カメラ名・レンズ名・露出値・生EXIF辞書を抽出（EXIF欠損時はファイル更新日時へフォールバック）。
- `build_filtered_exif_bytes(meta, exif_mode, corrected_dt, copyright_str)`: 保存モード①〜④とオフセット補正後日時・Copyrightを反映して `piexif.dump` バイナリを生成（`ImageOps.exif_transpose` 適用後のため `Orientation` は `1` に正規化）。

### D. GUI・状態管理・並列バッチ実行層
- `DnDCTk(ctk.CTk, TkinterDnD.DnDWrapper)`: `customtkinter` と `tkinterdnd2` を多重継承で統合するベースウィンドウクラス（`TkinterDnD._require(self)` を使用）。
- `FontPickerDialog(ctk.CTkToplevel)`: インクリメンタル検索とリアルタイム文字描画プレビューを備えたフォント選択ダイアログ。
- `SliderEntryWidget(ctk.CTkFrame)`: `CTkSlider` と `CTkEntry` の双方向同期および変更コールバック発火を行う共通コンポーネント。
- `PatternListItem(ctk.CTkFrame)`: 同時出力パターン一覧の1行ウィジェット（選択状態管理・ショート表記サマリー更新）。
- `PhotoDeliveryApp(BaseWindowClass)`: メインアプリクラス。
  - **UI構築・固定ロック**: `_build_ui` ほか。`pack_propagate(False)` と `CTkOptionMenu(dynamic_resizing=False)` により、項目切替時のレイアウト揺れを防止。
  - **プレビュー制御**: `schedule_preview_update()`（45msデバウンス）→ `_render_live_preview_now()`。プレビュー元画像は `_cached_single_source` / `_cached_dual_sources` にサムネイル化してメモリ保持し、スライダー操作時のディスクI/Oを排除。
  - **並列バッチ出力**: `start_batch_export()` 内の `worker()`（`threading.Thread`）から `ThreadPoolExecutor` で `process_one()` を並列実行し、全パターンのJPEG保存・タイムスタンプ同期・ZIP圧縮を行う。
  - **状態永続化**: `_build_state_dict()` / `_apply_state_dict()` / `_restore_autosave_if_exists()` / `_on_close_window()` によるJSONプリセット入出力およびオートセーブ。

---

## 4. 現時点の課題とアップデート時の注意点・ボトルネック
1. **macOS実機向けビルド・配布の未完了課題（保留中）**
   - GitHub Actions上での `.app` / `.dmg` ビルドにおいて、Apple Silicon (`arm64`) と Intel (`x86_64`) のアーキテクチャ差異、PyInstallerにおける `--onefile` と `--windowed` の併用制限、およびGatekeeperのアドホック署名・検疫まわりの実機起動検証が未完了（現在はWindows `.exe` 運用を優先）。
2. **ワーカースレッドからのTkinterウィジェット直接操作（スレッドセーフティ）**
   - `start_batch_export` 内の別スレッド `worker()` から `self.progress_bar.set()` や `self.status_label.configure()`、`self.run_btn.configure()` を直接呼び出している。Windowsでは動作しているが、macOS（Cocoa Tcl/Tk）や将来のPython/Tkバージョンではクラッシュ要因となるため、改修時は `self.after(0, ...)` 経由のメインスレッド更新へ寄せるのが安全。
3. **大量ファイル読込時のメインスレッド同期EXIFスキャン**
   - `_scan_cameras_in_files()` が読み込み時に全画像へ `extract_exif_metadata()` をメインスレッド上で同期実行し、さらに書き出し開始時にも再スキャンしている。数百〜数千枚規模のフォルダを投入した際にUIが一時フリーズするボトルネックになり得るため、メタデータのキャッシュ化または非同期スキャン化が今後の改善候補。
4. **`piexif.dump()` の「③完全維持」モードにおけるカメラ固有タグ例外**
   - 一部メーカーのMakerNoteや特殊型タグを含むJPEGで `piexif.dump()` が型エラーを起こすケースがあり、現状は `except Exception: return None` で握り潰してEXIFなし保存へフォールバックする仕様になっている。
5. **`tkinterdnd2` と `customtkinter` の内部API依存**
   - `DnDCTk` クラスで内部メソッド `TkinterDnD._require(self)` を呼んでいるため、両ライブラリのメジャーアップデート時に互換性確認が必須。
6. **UI揺れ防止ロックの維持**
   - 左右ペイン・各カードの `pack_propagate(False)`、プルダウンの `dynamic_resizing=False`、プレビューの `place_on_fixed_stage()` はレイアウトのブレを防ぐための必須設計であり、UI改修時にこれらを解除しないこと。
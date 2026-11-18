# Instant Delivery & SNS Studio (v4.3.2) 仕様・構造ドキュメント (SPEC.md)

## 1. アプリの目的と主な機能
イベント・ライブ撮影等におけるクライアント即納用データおよびSNS公開用データを、1回の読み込み（1パス）で複数パターン同時に並列生成するデスクトップ画像処理ツール。

### 主な機能
- **複数パターンの1パス並列一括書き出し**: 元比率（WMなし/あり）、SNS用比率（1:1 / 4:5 / 9:16）など複数の出力ルールをリスト管理し、`ThreadPoolExecutor` で並列JPEG出力（4:4:4 `subsampling=0`、ICCプロファイル維持）。
- **枠・背景ブラー生成エンジン**: 白枠・黒枠・四方余白調整に加え、元画像の四隅10%を自動クロップして周辺減光を除去した「背景ブラー（明・すりガラス / 標準 / 暗・シネマ風）」および明るさ・ぼかし量のスライダー微調整に対応。
- **マルチスロット・ウォーターマーク合成**: 1〜20枠まで動的増減可能（最大20枠まで同時配置可能）。透過PNG（余白自動トリム・明背景時の暗色反転対応）およびテキスト（PC内フォント検索・ローカル `fonts` フォルダ参照・文字間隔トラッキング調整）に対応し、文字/ロゴ本体の端を基準とした余白計算とドロップシャドウ付与を行う。
- **枠外EXIFテキスト印字**: カメラ名・レンズ名・露出情報（焦点距離 / F値 / SS / ISO / 撮影日）・自由追記テキスト（フィルム名等）を「センター2段」または「左右スプリット」で下部余白に印字。メーカー表記等の文字列置換ルールにも対応。
- **EXIFフィルタリング再構築 ＆ 複数カメラ時刻ズレ補正**: 読み込みフォルダ内のカメラ機種を自動検出し、分・秒単位の時刻オフセットを加算してEXIF撮影日時順（ミリ秒 `SubSecTimeOriginal` 対応）で連番リネーム・ソート。EXIF保存は4段階（①納品最適[日時のみ] / ②カメラ維持 / ③完全維持 / ④全削除）＋Copyright埋め込み・OSファイル更新日時同期（`os.utime`）に対応。
- **3モード切替リアルタイム・プレビュー**: 「全パターン比較（2×2グリッド）」「縦横向き比較（片方向のみ読込時は中央クロップで逆向きを仮想生成）」「1枚拡大」を45msデバウンス制御でリアルタイム描画。
- **設定管理・オートセーブ**: JSON形式でのプリセット保存・読込に加え、ウィンドウ終了時および書き出し実行時に `autosave_config.json` へ全UIステートを自動保存し、次回起動時に自動復元する。
- **フォルダ別ZIP自動生成**: 書き出し完了後、各出力パターンフォルダを個別にZIPアーカイブ化。

---

## 2. 動作環境と使用フレームワーク・ライブラリ
- **言語・想定ランタイム**: Python 3.12（Windows 10/11 実機稼働確認済、macOS クロスプラットフォーム対応コード実装済）
- **依存ライブラリ (`requirements.txt` で上限・下限を管理)**:
  - `customtkinter>=5.2.2,<6.0.0` + 標準 `tkinter`（ファイル選択ダイアログ拡張子指定はWin/Mac共通のスペース区切り方式）
  - `Pillow>=10.4.0,<14.0.0`
  - `piexif==1.1.3`
  - `tkinterdnd2>=0.3.0,<0.5.0`（未導入環境では自動でボタン選択UIへフォールバック）
  - `pyinstaller>=6.10.0,<7.0.0`
- **CI / パッケージング (`.github/workflows/build.yml`)**:
  - Node.js 24 対応アクション（`actions/checkout@v5`、`actions/setup-python@v6`、`actions/upload-artifact@v6`）を使用。
  - Windows (`windows-2025`): `--onefile --noconsole` で単一 `.exe` を生成。
  - macOS (`macos-15` [arm64] / `macos-15-intel` [x86_64]): PyInstaller v7.0 仕様に準拠し、`--windowed`（`onedir` 形式）で `.app` を生成後、アドホック署名と `hdiutil` による `.dmg` パッケージ化を実行。

---

## 3. アーキテクチャ（主要クラス・関数と役割の切り分け）
単一ファイル `app.py`（約2,760行）内に、以下の4層を分離して実装している。

### A. OS・パス解決・ユーティリティ層
- `_resolve_storage_dirs()`: スクリプト実行時とPyInstaller凍結時（`sys.frozen`、macOS `.app` バンドル階層含む）の双方で実行ファイル横のディレクトリを特定。さらに書き込みテストを行い、読み取り専用環境（DMG内からの直接起動や保護フォルダ配下）ではOS標準のユーザーデータ領域（Mac: `~/Library/Application Support/InstantDeliveryStudio` / Win: `%LOCALAPPDATA%\InstantDeliveryStudio`）へ `LOCAL_FONTS_DIR` と `AUTOSAVE_FILE` を自動退避する。
- `open_folder(path_str)`: Windows (`os.startfile`) / macOS (`open`) / Linux (`xdg-open`) 対応のフォルダ展開関数。
- `scan_pc_fonts(force_refresh)` / `load_font(font_path, size)`: OS別のシステム・ユーザーフォントフォルダおよび `LOCAL_FONTS_DIR` / `BUNDLED_FONTS_DIR` をスキャンして `_SYSTEM_FONTS_CACHE` に保持。

### B. 画像処理・レンダリング層（ステートレス関数群）
- `render_single_pattern(base_img, meta, pattern, wm_configs, caption_cfg, preview_max_edge)`: 1枚の画像に対する単一パターンの統合レンダリング関数。
- `generate_blurred_background(...)`: 中央80%クロップ＋`GaussianBlur`＋モード別色調補正による背景ブラー生成。
- `create_text_watermark(...)` / `apply_drop_shadow(...)` / `paste_watermarks_on_surface(...)`: トラッキング付きテキスト画像生成、短辺基準のドロップシャドウ生成、および透明パディング量を差し引いた四隅へのアルファ合成。
- `place_on_fixed_stage(...)` / `make_simulated_opposite_orientation(...)` / `create_sample_dummy_image(...)`: プレビュー時のUI揺れを防ぐ固定台紙配置、仮想縦横クロップ生成、ダミー画像生成。

### C. EXIFメタデータ処理層
- `_safe_decode_exif_str(raw_val, default)` / `extract_exif_metadata(img_path)`: `bytes` / `str` 両対応の安全なデコードとタプル長チェックを行い、日時・ミリ秒・カメラ名・レンズ名・露出値・生EXIF辞書を抽出（EXIF欠損時はファイル更新日時へフォールバック）。
- `build_filtered_exif_bytes(meta, exif_mode, corrected_dt, copyright_str)`: 保存モード①〜④すべてを `try ... except` で保護し、不正タグ混入時でもスレッドを止めずに `None`（EXIFなし保存）へフォールバックする。

### D. GUI・状態管理・並列バッチ実行層
- `DnDCTk(ctk.CTk, TkinterDnD.DnDWrapper)`: `customtkinter` と `tkinterdnd2` の統合ベースクラス。
- `FontPickerDialog(ctk.CTkToplevel)` / `SliderEntryWidget(ctk.CTkFrame)` / `PatternListItem(ctk.CTkFrame)`: フォント選択ダイアログ、スライダー・数値入力双方向同期部品、パターン一覧行部品。
- `PhotoDeliveryApp(BaseWindowClass)`: メインアプリクラス。
  - **UI固定ロック**: `pack_propagate(False)` と `CTkOptionMenu(dynamic_resizing=False)` により項目切替時のレイアウト揺れを防止。
  - **プレビュー制御**: `schedule_preview_update()`（45msデバウンス）→ `_render_live_preview_now()`。
  - **スレッドセーフ並列バッチ出力**: `start_batch_export()` 内の `worker()` から `ThreadPoolExecutor` で並列JPEG保存・ZIP圧縮を実行。進捗・完了・エラー表示はすべて `self.after(0, ...)` 経由でメインスレッドから更新し、`try ... finally` によって例外発生時でも必ず実行ボタンが有効状態に復帰する。
  - **状態永続化**: `_build_state_dict()` / `_apply_state_dict()` / `_restore_autosave_if_exists()` / `_on_close_window()` によるJSONプリセット入出力およびオートセーブ。

---

## 4. 解決済み項目と今後の監視・改善ポイント
### v4.2 にて対応完了済みの項目
- `requirements.txt` のバージョン上限設定および `build.yml` での参照統一
- GitHub Actions の Node.js 24 対応版（`checkout@v5` / `setup-python@v6` / `upload-artifact@v6`）への更新
- macOS ビルドの `onedir`（`--windowed`）移行および読み取り専用起動時のユーザー領域フォールバック（`_resolve_storage_dirs`）
- ワーカースレッドからのUI更新の `self.after(0, ...)` 化と、`try ... finally` による実行ボタン復帰保証
- EXIF全モードの例外保護（`build_filtered_exif_bytes`）およびタグ型安全性強化（`_safe_decode_exif_str`）
- ファイル選択ダイアログ（`filedialog`）の拡張子フィルターをWindows/macOS共通のスペース区切り書式に統一

### v4.3 にて対応完了済みの項目
- **プレビュー画面の動的リサイズ追従（案 A）**: ラップトップ等の小画面でもプレビュー画像や操作ボタンがはみ出して切れないよう、`preview_stage` の実測寸法から表示寸法を自動計算し、従来寸法（全パターン比較 350x275 / 縦横向き比較 580x295 / 1枚拡大 680x580）を上限として縮小。`<Configure>` イベントによるデバウンス付き再描画に対応。ウィンドウ最小サイズも `1100x580` に緩和し、起動サイズは画面解像度でクランプ。寸法計算は表示スケーリング（DPI）を論理ピクセルに換算して行う。
- **ウォーターマークスロットの動的増減（最大20枠）**: 従来4枠固定だった透かしスロットを、1〜20枠の間で自由に追加・削除できるよう改修（`_create_wm_slot` への構築処理の切り出し、スロット選択のドロップダウン化）。保存・復元およびオートセーブも動的件数に対応。各スロットには任意の名前（イベント名など）を付けられ、名前は設定に保存される（表示専用で、描画には影響しない）。

### v4.3.1 にて対応完了済みの項目
- **EXIF読込キャッシュと縦横向き比較キャッシュの有効化**: 読込時のカメラ検出スキャンで取得したEXIFを `_exif_cache` に保持し、プレビューと書き出しで再利用（従来は同じEXIFを複数回読み直していた）。縦横向き比較では未使用だった `_cached_dual_sources` と逆向き探索結果のキャッシュを有効化し、片方向のみ大量にあるフォルダでも再描画が即座に返るようにした。
- **ファイル・フォルダ名のサニタイズと同名衝突回避**: イベント名・パターンのフォルダ名・元名維持時のファイル名に `_sanitize_fs_name` を適用し、Windows 禁止文字や予約名による書き出し失敗を防止。フォルダ名とファイル名が重複する場合は連番を付けて上書きを回避する。
- **ZIP圧縮の対象限定**: ZIPには今回書き出したファイルのみを含める（従来はフォルダ内の既存ファイルも混入していた）。
- **全パターン比較の空白セル表示クリア**: パターン削除後に残っていた空白セルのクリック判定と画像参照をクリア。
- **表記置換の空キー防御**: `_collect_caption_config` で左辺が空の置換ルールを除外し、置換文字が1文字ずつ挟まる事故を防止（描画側は無変更）。
- **プリセット保存・読込の例外処理**: 保存失敗時と破損JSON読込時にエラーダイアログを表示（オートセーブの静音動作は維持）。
- **EXIF表記の整形**: スローシャッター（0.8秒等）が `1/1s` と表示される問題と、ISOがタプルで返る機種での表示を修正。
- **細かなUI改善**: フォント直接選択ダイアログに `parent` を指定して背面隠れを防止。透かしスロット選択ドロップダウンの幅を広げ、長いスロット名でも見切れにくくした。

### v4.3.2 にて対応完了済みの項目
- **ZIP化チェックの初期値をオフに変更**: 納品はフォルダのままファイル転送サービスで送る運用が多いため、新規・リセット時の初期値をオフにした（オートセーブに保存済みの値がある場合はそちらが復元される）。
- **矢印キーでのプレビュー送り**: ルートに `<Left>` / `<Right>` をバインドし、既存の前後ボタンと同じ処理（`_prev_photo` / `_next_photo`）で写真を送れるようにした。テキスト入力欄にフォーカスがある場合は発動しない。
- **ドラッグ&ドロップ受付の画面全体化**: 受付を `drop_label` のみから、ウィジェットツリーを再帰的に登録する方式（`_register_drop_recursive`）へ拡張し、UI構築時・プレビュー描画後・ファイル読込後に画面全体でドロップを受け付ける。
- **ロゴ画像とフォントの読み込みキャッシュ**: 透過PNGロゴの読込（開いて余白トリムした画像）と `load_font` のフォントオブジェクトをモジュールレベルのキャッシュで再利用し、描画ごとの再読込をなくした。`paste_watermarks_on_surface` の変更は読込部の呼び出し置換のみで、描画結果は従来と同一。書き出し開始時には有効なPNGスロットのロゴを先読みする。

### 今後の監視・検証項目
1. **macOS実機での `onedir` 版 `.dmg` 起動・ファイル選択確認**: Apple Silicon / Intel 実機での初回起動（Gatekeeper解除手順含む）およびフォント・画像選択ダイアログの動作確認。
2. **`tkinterdnd2` の `_require` 内部API依存**: バージョン更新時の起動・ドロップ動作確認。
3. **GitHub Actions `macos-15-intel` ランナーの提供期限**: 2027年8月頃の提供終了に向けたUniversal2化または構成見直し。

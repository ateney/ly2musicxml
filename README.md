# ly2musicxml

LilyPond (`.ly`) の楽譜ソースを **MusicXML** に変換するツールです。

- **Web 版** (GitHub Pages): ブラウザ内で完結、ファイルはどこにも送信されません
- **CLI 版** (`ly2musicxml.py`): Python 3.7+ / 依存ライブラリなし

## 使い方

### Web 版

[ページを開く](https://ateney.github.io/ly2musicxml/) → `.ly` の内容をペースト（またはファイルを選択）→ 「変換する」→ ダウンロード。

### CLI 版

```console
python ly2musicxml.py movement-1.ly
python ly2musicxml.py movement-1.ly -o thefirst.musicxml
python ly2musicxml.py movement-1.ly --title "String Quartet No. 8" --composer "D. Shostakovich"
```

## 対応機能

| カテゴリ | 内容 |
| --- | --- |
| 音程 | `\relative` 相対音程（和音内相対、オクターブ記号 `'` `,`、基準は記入通り） |
| 音符 | 和音、タイ（小節をまたぐ連鎖）、スラー、アーティキュレーション（`-.` `->` `--` `-_`） |
| リズム | 付点、連符 `\tuplet`、`\partial` ピックアップ小節、`\repeat unfold`（本体のみの繰り返し含む） |
| 記号 | 調号・拍子・テンポの変更、強弱記号、ヘアピン（重複スパナの番号付けと暗黙終了）、音部記号、`\ottava`（譜刻用・実音不変） |
| 構成 | `<name>_begin` ブロックと `<name>_<part>` 変数を自動検出、複数楽章は楽章ごとに書き出し |

MusicXML 3.1 (score-partwise) を出力します。MuseScore / Finale / Sibelius / Dorico で開けます。

## 検証

シュスタコーヴィチ弦楽四重奏曲第8番 Op.110（Jonathan Chen による CC ライセンスの LilyPond 譜面）の第1・2楽章で、変換結果を **LilyPond 本体の MIDI 出力と全パート完全一致**（全音符の発音時刻・音高・演奏長）で突き合わせ検証しています。

## ライセンス

MIT License — `LICENSE` を参照。

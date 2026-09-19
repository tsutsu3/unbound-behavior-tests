# unbound-behavior-tests

[English](README.md) | 日本語

Unbound の設定まわりの挙動を、**ソースを読んで確定した事実**と
**実際に動かして確認した結果**の両方で記録する、実行可能なケース集。

対象: Unbound **1.26.0**（tag `release-1.26.0`, commit `a45da353d3feb5d8fc00685fa1ceda3816d5108f`）

範囲: 設定ファイルのパース仕様、`local-zone` / `local-data`、`forward-zone` / `stub-zone`。

## 使い方

Docker Compose を使う。作業ツリーが `/work` にマウントされるので、
`cases/*.yaml` やランナーを編集してもイメージを作り直す必要はない。

```sh
docker compose build                     # 初回、および Dockerfile / uv.lock を変えたとき
docker compose run --rm tests            # 全ケースを実行する
```

`tests` に渡した引数はそのまま pytest の引数になる。

```sh
docker compose run --rm tests -k forward-addr-port-overflow   # 1 ケースだけ
docker compose run --rm tests --run-unverified                # needs-test も合否に含める
docker compose run --rm tests -vv --color=yes                 # 詳しく出す
```

付録の表を生成する。出力は `out/` で、このリポジトリにコミットする。
英語版が `*.md`、日本語版が `*_ja.md` で、どちらも同じケースファイルから作る。

```sh
docker compose run --rm gen
```

lint と format は Ruff を使う。

```sh
docker compose run --rm lint                    # ruff check .
docker compose run --rm lint format --check .
docker compose run --rm lint check --fix .      # 自動修正
docker compose run --rm lint format .           # 整形
```

コンテナの中を触る。`unbound` / `unbound-checkconf` / `dig` / `pytest` / `ruff` が入っている。

```sh
docker compose run --rm shell
```

`gen` と `lint` はホスト側のファイルを書くので、ホストのユーザーで動く。
uid/gid が 1000 でない環境では渡して上書きする。

```sh
UID=$(id -u) GID=$(id -g) docker compose run --rm gen
```

Compose を使わない場合。

```sh
docker build -t unbound-behavior-tests:1.26.0 .
docker run --rm -v "$PWD:/work" -w /work unbound-behavior-tests:1.26.0 pytest -q
```

ホストに直接 uv がある場合、`gen.py` と Ruff は Unbound を必要としないので
コンテナ無しでも動く。ケースの実行には Unbound 1.26.0 が要るのでコンテナを使うこと。

```sh
uv run python gen.py
uv run ruff check .
uv run ruff format .
```

## ケースの書き方

1ケース = ファイル1つ = テスト1本 = 付録の1行。
ケースごとにテストコードは書かない。`test_behavior.py` は 1 関数だけで、
`cases/*.yaml` を走査して `pytest.mark.parametrize` に流す。

```yaml
id: forward-addr-port-overflow          # ファイル名と一致させる
title:
  en: "The forward-addr @port is not range-checked and is truncated to uint16"
  ja: forward-addr の @ポートは範囲検証されず uint16 に切り詰められる
chapter: "6.4"                          # 書籍の節番号。未定なら TBD
category: A                             # A / B / C / D / undecided
versions: ["1.26.0"]
source:
  en: "util/net_help.c authextstrtoaddr() does port = atoi(s+1) without a range check"
  ja: "util/net_help.c authextstrtoaddr()。atoi() に範囲検証がない"
manual:                                 # man の該当箇所。無ければ none で始める
  en: "none"
  ja: "none"
layers:                                 # 1 要素ならコラムにしない
  en: ["address parsing"]
  ja: ["アドレス分解"]
status: verified                        # verified / needs-test / inconclusive
config: |
  forward-zone:
    name: "."
    forward-addr: 127.0.0.1@70000
command: run-and-observe
expect:
  checkconf_exit: 0
  listen:
    proto: udp
    addr: "127.0.0.1:4464"
  query: "example.com. A"
  note:
    en: "70000 is truncated to uint16 and becomes 4464"
    ja: "70000 は uint16 に切り詰められ 4464 になる"
```

### 英語と日本語

説明文のフィールド `title` / `source` / `manual` / `layers` / `expect.note` は、
`en` と `ja` の 2 つのキーを**この順で**持つマップにする。片方だけは書けない。

- `layers` は両言語で要素数を揃える。同じ位置の要素が同じ層を指す。
- man の原文の引用は英語のまま両方に入れる。訳さない。
- `config` / `files` / `expect` の値（`note` を除く）は言語に依存しないので 1 つだけ書く。

`config` は `runner/unbound.py` の定型部（`server:` 節）の**後ろに連結される**ので、
`server:` のオプションをそのまま書いても、`forward-zone:` のような
トップレベル節を開いてもよい。

### 複数ファイルを使うケース

`include:` / `include-toplevel:` を試すケースは、任意指定の `files:` に
「相対パス → 内容」のマップを書く。生成した `unbound.conf` と同じディレクトリに
展開され、そのディレクトリを作業ディレクトリにして
`unbound` / `unbound-checkconf` が起動するので、相対パスでそのまま参照できる。

```yaml
config: |
  include: "conf.d/*.conf"
files:
  conf.d/10-first.conf: |
    server:
        local-zone: "example.com." static
  conf.d/20-second.conf: |
    server:
        local-zone: "example.com." refuse
```

パスは一時ディレクトリの中に閉じる。絶対パスと `..` を含むパスは読み込み時に拒否され、
`unbound.conf` という名前も使えない（`config:` から生成する名前と衝突するため）。

### command と expect

| command             | 何をするか                                  | expect に書けるキー                                                                                            |
| ------------------- | ------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `unbound-checkconf` | 設定を書いて `unbound-checkconf` を実行する | `exit`, `stderr_contains`, `note`                                                                              |
| `run-and-observe`   | daemon を起動し、偽の上流に届くかを見る     | `checkconf_exit`, `listen{proto,addr}`, `query`, `note`                                                        |
| `run-and-dig`       | daemon を起動し、応答の中身を見る           | `checkconf_exit`, `query`, `rcode`, `answer_contains`, `answer_count`, `answer_ttl`, `stderr_contains`, `note` |

`run-and-dig` の `stderr_contains` は daemon を止めてから読む。
`verbosity: 4` を設定に書けば内部の詳細ログを判定に使える
（`unbound-checkconf` はグローバル `verbosity` を設定しないので、これは daemon でしか見えない）。

### status と pytest の対応

| status         | 実装                                                              |
| -------------- | ----------------------------------------------------------------- |
| `verified`     | 実行して比較する。食い違えば失敗                                  |
| `needs-test`   | skip。`--run-unverified` を付けたときだけ合否に含める             |
| `inconclusive` | 常に skip。答えではなく「未確定である」ことを記録するためのケース |

### スキーマは勝手に広げない

`runner/cases.py` が読み込み時に検査する。

- 未知のキーはエラー。`command` ごとに `expect` に書けるキーも固定。
  最上位の任意指定キーは `files` だけ。
- `id` はファイル名と一致していること。
- 説明文のフィールドは `en` / `ja` の両方が必要。`layers` は要素数が一致していること。
- `category` が `B` か `C` なら、`manual.en` が `none` で始まるものは許さない
  （B も C も **man についての主張**なので、原文を引かずには主張できない）。
- 引用符で囲まれていない値に ` #` が含まれていたらエラー。
  YAML がコメントとして食ってしまい、付録の行が途中で切れるため。

表現できないケースが出たら、拡張案とその理由を先に報告してから変更すること。

## 根拠の種別を混ぜない

- **Source** — ソースを読んで確認した。ファイルと関数を記録する。
- **Test** — 実際に 1.26.0 を動かして確認した。
- **Manual** — man に記述がある。**原文を転記する。**
- **未確認** — どれでもない。

Source だけの主張を Test 済みとして書かない。逆も同じ。
`expect` は**先にソースから予測して書く**。実行結果から埋めない。

## ディレクトリ

| パス                 | 中身                                     |
| -------------------- | ---------------------------------------- |
| `cases/`             | ケース定義                               |
| `runner/cases.py`    | YAML の読み込みとスキーマ検証            |
| `runner/unbound.py`  | 設定生成と Unbound の起動・問い合わせ    |
| `runner/upstream.py` | 偽の上流サーバー（宛先の観測）           |
| `test_behavior.py`   | pytest のエントリ（1 関数）              |
| `gen.py`             | 付録一覧表と集計の生成（英語・日本語）   |
| `out/`               | `gen.py` の出力。コミットする            |
| `README.md`          | この README の英語版                     |
| `references/`        | Unbound のチェックアウト。**Git 管理外** |

`references/` の用意:

```sh
git clone --depth 1 --branch release-1.26.0 \
  https://github.com/NLnetLabs/unbound.git references/unbound-1.26.0
```

テストの実行には不要（Docker イメージがリリース tarball からビルドする）。
ケースが引用している行番号を追うときに使う。

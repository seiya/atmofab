# 確認手順(met-dsl)

`cd /home/seiya/work/met-dsl` で実行する。**測っていない断言を commit や TODO.md に書かない**。

## スイート

```bash
TMPDIR=/dev/shm python3 -m pytest tools/tests/ -q -p no:randomly
```

- **並列で2本走らせない。** `TMPDIR` を共有するため偽 fail が出る(実際に1度踏んだ)
- 基準線は `origin/main` の実測値。`git worktree` / `git archive` で比較する場合、`$HOME` の外に
  置くと `test_hooks_common.py::ForbidBackendCredentialReadTests` が落ちる(`../..` / `~` が
  home に解決される前提のテスト。パス深度依存の既存挙動で、`main` でも branch でも同じ)。
  **絶対値でなく DELTA を比べる**
- **この件数は腐る。渡す前に測り直す。** 2026-08-13 は5件、**2026-08-18 実測は2 failed + 1 skipped**
  (`test_blocks_bash_only_tilde_prefixes` / `test_directory_options_anchor_like_cd`、`origin/main`
  でも同一)。テストが増減すれば当然変わるので、**この行の数字を引用せず自分で1回測る**
- **レビュアに渡す散文にもこれを書く。** PR #57 では3体のレビュアが各自これを再導出し、うち1体は
  私の申告を実測で訂正してきた。書いておけば済む。逆に**古い件数を渡すと、レビュアはそれを
  baseline だと思って本物の失敗を見逃す**
- 恒久 skip は 1 件(較正テスト)。それ以外の skip が増えたら理由を確認する

## ツリー全体の差分(ゲートを変えたら必ず)

**この repo で最も効く確認。** ゲートを実物のコーパスに直接当てて、変更前後の verdict を比べる。
L128 では19コミット・9ラウンドを通じて「29→27・`problem/` ドメインのファイルは1つも verdict が
変わらない」を毎コミット確認でき、レビュアもこれを独立に再現した。**「壊していない」を
議論でなく差分で示せる。**

```bash
# 1. ゲートを直接呼ぶスキャナを書いて JSON に落とす(ファイル -> 違反リスト)
#    node_key / dep_spec_ids など gate の前提は明示的に固定する
python3 corpus_scan.py > after.json
git stash -q && python3 corpus_scan.py > before.json && git stash pop -q
# 2. 件数ではなく「どのファイルのどの違反が動いたか」を出す
```

- **ハーネスを必ず記録する。** dep_spec_ids の導出を変えると絶対値が変わる(同じツリーで
  29→27 / 31→29 / 35→33 になった)。**差分は再現するが絶対値はハーネス依存**。
  数値を TODO や commit に書くなら導出方法も一緒に書く
- **subroutine 粒度まで見る。** ファイル単位の flag/silent だけ見ると、同じファイル内で
  片方が消えて片方が残る動きを見落とす(実際に見落とした)
- fail-closed 方向の変更(拒否が増える向き)も同じ方法で測る。L128 では
  **357 ディレクトリの module dep map が byte 一致・Makefile verdict 103/103 一致**を示した

## ruff は「baseline と同一」を示す

件数ではなくファイル単位で照合する。**照合対象ファイルを増やしたら baseline も取り直す** —
2ファイルで測った「同一の1件」を、3ファイル目を足した後もそのまま書いて誤りになった。

```bash
for f in <touched files>; do echo -n "$f: "; ruff check "$f" 2>&1 | tail -1; done
git stash -q -u && git checkout -q origin/main -- . \
  && for f in <touched files>; do echo -n "$f: "; ruff check "$f" 2>&1 | tail -1; done
git checkout -q HEAD -- . && git stash pop -q
```

## doc サイズ上限

leaf の文脈に入る doc には上限テストがある。`docs/workflow/phases/*.md` を触ったら:

```bash
TMPDIR=/dev/shm python3 -m pytest tools/tests/test_orchestration_runtime.py -q -p no:randomly -k child_context_docs
```

超えたら**上限を上げるのではなく冗長を削る**。

**このテストは最大値なので、doc を削る変更では構造上絶対に落ちない** = 削る作業をしている間、
このテストは何も言っていない。緑を根拠にしないこと。**残量を測って書く**:

上限値は**書き写さず、テスト側の表から読む**(9 doc 分あり、ばらばらに bump されている):

```bash
python3 - <<'PY'
import pathlib, sys, importlib
sys.path.insert(0, "tools/tests")
C = importlib.import_module("test_orchestration_runtime").ChildContextDocSizeTests._CEILINGS
for rel, ceil in sorted(C.items()):
    n = pathlib.Path(rel).stat().st_size
    print(f"{n:6d}  headroom {ceil - n:+6d}  {rel}")
PY
```

PR #55 では SKILL を削る作業の最中に headroom が **1 byte** まで落ちていたのに数ラウンド
気づかなかった(最終 37)。**2026-08-13 実測では 9 doc 中 4 つが headroom 50 未満**
(`workflow-generate-generate` +6、`workflow-generate-verify` +5、`AGENT_CONTRACT` +47、
`phase_01_compile` +50)。**これらは一文足すだけで落ちる。** 触る前に測る。

## 実サーバプロセス経由の end-to-end

`import` ではなく `mcp_call.py` 経由で確かめる(JSON-RPC 層と env の扱いを含めるため)。

```bash
# standalone は動く
env -u METDSL_WORKFLOW_MODE -u METDSL_ORCHESTRATION_ID \
  python3 mcp_servers/mcp_call.py --tool run_syntax_check --args-json '{"project_dir": "<abs>"}'
# workflow 下で orchestration_id を落とすと拒否
METDSL_WORKFLOW_MODE=1 python3 mcp_servers/mcp_call.py --tool run_linter --args-json '{"project_dir": "<abs>"}'
```

## LLM CLI の実挙動(無課金・capture harness)

leaf の起動フラグ・設定層・権限層・注入内容を変える作業では、**フラグの意味から推論せず捕獲する**。
`ANTHROPIC_BASE_URL` をローカル HTTP サーバに向けると、request body がそのまま読める。
issue #63 ではこれで hook 発火・`CLAUDE.md` 注入の有無・permission verdict・`--resume` の可否・
matcher の意味論・home に何が書かれるかを全部無課金で決めた。

```python
# 骨格: /v1/messages を受けて body を保存する。side request(tool roster が空)は
# 素の end_turn で流し、MAIN request(target tool を持つ)にだけ合成 tool_use を返す。
# 2通目の tool_result が permission layer の verdict。
```

- **対照を必ず1本混ぜる。** 「全部通った」と「層が死んでいた」は対照なしには区別できない。
  権限なら**必ず拒否される形**(`curl` など)、注入なら**旧フラグでの同じ測定**
- **測る leaf の種類を本番に合わせる。** tool 無しの leaf と tool 持ちの leaf では、CLI が
  config dir に作る物が違った(6個中2個を取りこぼした)
- **`--debug-file` を付ける。** `tool_dispatch_end ... outcome=ok` / `Bash tool permission denied` /
  `Applying permission update: ... destination 'userSettings'` が verdict の直接証拠になる
- **sentinel を書く hook** を設定に入れると、hook が発火したかが痕跡で分かる
  (`Found 0 total hooks in registry` は**プラグイン**の話で、settings の hook とは別)
- cwd と `CLAUDE_CONFIG_DIR` は scratch に置く。**repo を cwd にすると本物の hook が動いて
  `workspace/orchestrations/` に書く**(実際に汚した)

## 散文の掃除(規則を変えたとき)

規則を**根拠として引用している**文を探す。docstring・コメント・実際に出力される違反メッセージ・
phase doc・skills・TODO の4層に散っている。

```bash
# 変えた仕組みの名前で引く(例: 行スキャンをやめた場合)
rg -n "line-scan|LINE-SCAN|行スキャン|線形スキャン" tools/ docs/ skills/
# 「実測」と書いてある主張は測り直す
rg -n "[Mm]easured|実測" tools/ docs/ | rg -i "<変えた対象>"
# 出力される文字列は docstring とは別物。violations.append / raise を直接見る
rg -n "violations.append|raise (ValueError|RuntimeError)" <touched file>
```

**測定値を根拠に書いたなら、変更後に測り直す。** PR #51 では同じ文字列を4回書き直した。

## 変異チェック

`~/.claude/skills/metdsl-review-loop/scripts/mutation_check.py` を使う。
手順は `metdsl-review-loop` の「出す前(ラウンド0)」が正典。

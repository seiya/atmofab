# 同じ事実を複数箇所が読んでいる組(既知)

新しい読み手を足すとき、または既存の読み手を変えるときは、**相方を探してから**変える。
PR #51 では、この形の欠陥が5件出た(うち1件は P1 相当)。判定は常に
「**leaf が書ける入力**で、2つの読みが食い違うか」。食い違いが安全側(厳しい方)に倒れるなら
残してよいが、その理由を書き残すこと。

## 現に存在するペア

| 事実 | 読み手 A | 読み手 B | 状態 |
|---|---|---|---|
| `impl_defaults.toolchain.build_system` | conductor `str(tc.get(...) or "make").lower()` | `_impl_defaults_toolchain_value`(構造読み・`.strip().lower()`) | 一致。B が厳しい側 |
| `impl_defaults.toolchain.language` | conductor `_conductor_authors_makefile` / `_conductor_authors_runner` | `_impl_resolved_language` | 一致。**元は行スキャナで、自由記述1行に乗っ取られた** |
| `build_system` 引数 | host gate(不在 → make) | server(不在かつ orchestration 下 → make) | 一致させた |
| `preset` 引数 | host gate(不在 → make_test、strip+lower) | server(固定表に完全一致) | 差は全て server 側で拒否に倒れる |
| `orchestration_id` の有無 | gate `is not None and strip()` | `_is_orchestrated_call` | 同一述語に統合済み |
| リポジトリルート | gate | 各包含チェック | `_repo_root_for_call` に統合済み。**workflow 下は server 自身のチェックアウト必須** |
| ソース一覧 | `_fortran_syntax_source_order`(自動探索) | `_validate_syntax_sources`(明示引数) | 統合済み。**本番は自動探索側しか通らない** |
| case_id 文法 | `_CASE_ID_TOKEN_RE`(Compile) | `_MAKE_NAME_VALUE_RE`(CASES 値) | 前者が後者の真部分集合 |
| `METDSL_WORKFLOW_MODE` | `tools/hooks/cli.py`(allowlist `{1,true,yes}` / `== "1"`) | server(空と `0` 以外は全て workflow) | server が最も広い = fail-closed 側 |

| leaf の設定層 | preflight `_read_repo_mcp_tool_permissions` / hook 配線を読む test 群 | `_prepare_claude_workflow_home` が pin してコピーする実体 | `leaf_config/claude/settings.json` に統合(issue #63)。**dev 層 `.claude/settings.json` とは sync test が hook を subset 照合 + grant を照合**。移行時、Bash 許可リストの pin だけ dev 層を読んだまま残り、**leaf 側の16 entry を全削除しても全緑**だった |
| leaf の transcript の置き場 | `_claude_session_resumable`(warm resume の可否) | `orchestration_diagnostics._leaf_transcript_path` / `_claude_projects_dir`(事後監査) | `tools/hooks/common.py::claude_leaf_projects_roots` に統合(issue #63)。**ただし用途で答が違う**: 監査は private home と operator home の両方を見るのが正しく、resume は**その launch が使う方だけ**。両方見る版は pre-move の session を resumable と答え、**存在しない home へ `--resume` を投げた** |
| 隔離 home の位置 | bwrap が bind する側(`claude_isolation_profile_kwargs`) | Bash read guard が禁じる側(`workflow_private_backend_homes`) | issue #63 で**片側だけ動かして**穴を開けた。operator の credential を private home に bind した結果、**同じ秘密が guard の知らないパスに現れた**(`~/.claude/...` は block、`<home>/...` は allow)。`_backend_runtime_bind_paths` の docstring が謳う「bind する集合 = guard が禁じる集合」は **`backend_rw_override` を渡す呼び出しには適用されない** |

## 読み手が3つ以上あるもの(ペアの表に収まらない)

**「相方を1つ探す」で止まると外す。** PR #55 では読み手を2回に分けて開いた結果、重篤度を
3回間違えた(規則 1-c)。読み手が3つ以上ある事実は、**開く前に全部列挙する**。

| 事実 | 読み手(全列挙) | 状態 |
|---|---|---|
| substep の出力集合 | conductor の `allowed_output_paths` 宣言 / runtime `compile_required`(membership、`_matches_phase_contract`)/ 派生 `allowed_file_tool_paths` / `output_manifest_write_guard`(hook)/ 終端 FS-diff / 各 `SKILL.md` の散文 | **定義が1箇所に無い。** PR #55 で `algorithm.summary.md` が3読み手で食い違っていた(SKILL が書けと言い、conductor は宣言せず、runtime は許可していた)。定義を1箇所に寄せる作業が未着手なので、外側のテストは**棄却のサンプルしか書けない** |
| harness が保存した tool-result の置き場 | **4つ**(2ファイル): `_is_persisted_tool_result_shape`(Bash の block 経路)/ `_blank_persisted_tool_results`(marker 走査の前処理)/ `cli.py` の auto-approve 走査2箇所 / `_is_persisted_tool_result_read`(Read tool) | issue #63 で全部が `~/.claude` 固定のまま取り残された。**block だけ直して「直った」と書いた** — auto-approve は別の呼び出しで、そこが id を渡さないままだったので、read は block されないが auto-approve もされず、committed permissions に `cat /tmp/...` が無いので結局読めない。**「拒否されなくなった」と「使えるようになった」は別の測定** |
| `agent_role` | **6つ**: 推論1(`build_capability_document`)/ skip 3(`_allowed_output_paths_for_launch` / `_validate_child_write_contract_preflight` / `_build_task_card`)/ `record_launch` 自身の fallback / conductor の `_register_codex_thread` の `or "substep"` | **CLOSED = PR #57**(2ヶ所の chokepoint で fail-closed + `prepare_launch_request_payload` 冒頭で正規化)。当初「5読み手」と見積もったが**実際は6**だった。詳細と実測は TODO.md の当該項目が正典 |

## 近縁の形: 同じ名前が2つの**別 payload** で届く

上の表は全て「1つの値を複数箇所が読む」形。**これとは別に、同じフィールド名が
**別々の入力**として届く形がある。読み手を全部列挙しても、payload を1つだと思っていると外す。

- `agent_role` は **launch request**(`record-launch`)と **終端 payload**(`record-agent-run` /
  `finalize-child`)の**両方**に載る。PR #57 の TODO 項目が書いていた修正方針は
  「record-launch で1回正規化すれば全読み手が同じ値を見る」だったが、
  `_validate_actual_write_paths`(監査本体)が読むのは**終端 payload の方**なので、
  **launch 側だけ直しても監査は閉じない**。chokepoint が2つ要ると分かったのはここ
- 判定手順: 読み手を列挙したら、次に**その読み手が読んでいる payload はどれか**を1つずつ確かめる。
  「同じキー名だから同じ値」は成り立たない
- 症状: 「全部の読み手を直したのに、ある層だけ挙動が変わらない」

## 意図的に統合していないもの

- `_impl_is_leaf_node` は行スキャンのまま。**インデント 0 の `dependency:` に錨を打っている**ため、
  他のキー配下にネストした値は物理行を列 0 に作れず、自由記述による乗っ取りが成立しない。
  変更するならこの前提ごと検証すること。

## 近縁の形: 同じ名前が1ファイル内で2回定義される(shadow)

「2箇所が同じ事実を読む」の兄弟で、**後の定義が前を黙って上書きする**形。TODO L118 が閉じた
`_split_top_level_commas` の二重定義(9,500行離れていた)がこのクラスで、L128 で**私が再発させた**:
`_FORTRAN_UNIT_OPEN` / `_FORTRAN_UNIT_END` を新規に定義したが、同じ名前が1,500行下に既存で、
私の定義は実行時に一度も使われなかった。テストが理解不能な落ち方をして初めて気づいた。

- `tools/validate_pipeline_semantics.py` は13,000行超。**新しいモジュール定数を足す前に名前を引く**
- 既存の定数が使えるなら使う。意味が違うなら**違いを名前に入れる**
  (`_FORTRAN_UNIT_OPEN` は `subroutine` も含む → host unit だけ欲しいなら `_FORTRAN_HOST_UNIT_OPEN`)
- 症状: 「パターンを直したのに挙動が変わらない」「変異を戻したのにテストが落ちない」→
  **コンパイル済みの値を実際に print して確かめる**(`print(vps._FOO.pattern)`)

## 探し方

```bash
cd /home/seiya/work/met-dsl
# ある事実を読んでいる箇所を洗う(例: toolchain の language)
rg -n "toolchain.*language|_impl_resolved_language|_conductor_authors" tools/ mcp_servers/
# 行スキャン(構造読みとの差が出る形)を探す
rg -n "splitlines\(\)" tools/orchestration_runtime.py
# 追加する名前が既にあるか / 重複定義がないか
rg -n "^_FORTRAN_MY_NEW_NAME\b" tools/
python3 -c "import tools.validate_pipeline_semantics as m; print(m._FORTRAN_UNIT_OPEN.pattern)"
```

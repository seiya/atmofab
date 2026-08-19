# 拒否を足したとき、それは誰の落ち度か

新しい拒否は必ずどこかに routing される。**帰属を間違えると、直せない相手に retry 予算を焼かせるか、
直せる相手の run を終了させる**。PR #51 では両方向を1回ずつやった。

正典は `docs/workflow/phases/phase_02_generate.md`(gate の失敗分類)と
`docs/ORCHESTRATION.md`(fail_closed の扱い)。ここには判断手順だけ置く。

## 判断

**その入力を書いたのは誰か** で決める。

| 原因 | 例 | routing |
|---|---|---|
| leaf が自分の write_root に書いたもの | `src/-o.f90` という名前、生成ソースの中身、IR の内容 | **content failure**(warm resume で当人が直す) |
| conductor が渡す引数 | `project_dir` が非絶対、capability token 空、`command_log_path` が範囲外 | **transport fail_closed**(leaf は直せない) |
| 環境・インフラ | mandatory compiler 不在、依存クロージャが壊れている、IR が読めない | **transport fail_closed** |

判断に迷ったら「**この leaf をもう一度走らせたら直る可能性があるか**」を問う。無いなら
content failure にしてはいけない。

## 例外は型で名指しする

`except ValueError` のような幅で拾うと、同じ関数が投げる**別の理由**まで同じ帰属になる。
PR #51 では `tool_run_syntax_check` の引数拒否(conductor 側の不備)が leaf の `syntax_error`
として計上され、`workspace/tmp` を別ディスクに symlink した構成で毎ノード retry 予算を焼く形になった。

```python
class SyntaxSourceNameError(ValueError):
    """leaf が書いた名前だけを表す。他の引数拒否は素の ValueError のまま。"""
```

捕まえる側は `except SyntaxSourceNameError`。**両方向を pin する**
(その例外は content になる / 他の ValueError は raise のまま)。

## content failure に落とすときの副作用

早期 return は、通常経路が必ず通る後処理を飛ばす。PR #51 では
`write_syntax_evidence`(host 側の証明書)を書かずに返し、しかも証明書リーダーが拒否する
stage 形(`fail` なのに command_id 無し)を記録していた。

- 通常経路の return と**同じキー**を返しているか
- 通常経路が書く成果物(証明書・ログ)を書いているか、書かない理由が説明できるか
- コンパイラや外部プロセスが**走っていない**なら stage は `skipped`(引用する command が無い)

## 新しい raise を足すとき、**囲んでいる handler が何のために書かれたか**を読む

帰属は「誰が入力を書いたか」で決めるが(上の表)、実際の routing は**その raise を捕まえる
`except` が既に持っている意味**に上書きされる。新しい例外は、既存 handler の分類を**継承する**。

PR #57 では `_register_codex_thread` に「壊れた launch request を surface する」つもりで
`RuntimeError` を足した。しかしその `try` の handler は **host 側の書き込み失敗**
(ENOSPC / 記録済み identity の衝突)のために書かれていて、transaction journal を復旧して
**transport-dead な `ProcResult` に変換する**。つまり「surface する」意図は
「課金済みの leaf を1本殺す」に化けていた。今回は raise が**どの書き込みよりも前**に起き、
record-launch の validator によって到達不能でもあるため実害なしと判定したが、
**それは読んで確かめた結果であって、書いた時点では分かっていなかった**。

- 新しい `raise` の**外側の `except` を必ず開く**。docstring があれば何のための handler か読む
- その handler の分類(transport / content / 復旧)が、自分の raise の帰属と**一致するか**
- 一致しないなら: 例外型を分けて handler 側で通す、raise 位置を handler の外へ出す、
  あるいは**一致しないまま出すと決めて理由を書く**(PR #57 は3つ目。レビュアに
  「意識的な判断として flag する」と言われた)

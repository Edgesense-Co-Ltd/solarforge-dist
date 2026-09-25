#!/usr/bin/env python3
# release.yml の門が効くことを PR の CI で見る番人（dist #7 の 1）。
#
# なぜ要るか: このリポジトリには PR で走る CI が無く、release.yml の門は
# 「入れたときに手元で測った」だけだった。次に触った人が門を壊しても
# workflow_dispatch を実際に起動するまで分からない。門3 の変異
# （exit 1 → exit 0）は**本文を更新しないまま rc=0 で成功を報告する**形なので、
# 起動しても気づけない可能性がある。
#
# 何を見ているか / 見ていないか:
#   見ている  … run: スクリプトの**分岐**（どの入力でどう終わり、gh を何回どう呼ぶか）
#   見ていない … 本物の gh release edit / create の振る舞い。GitHub 上の副作用。
#                したがってこれが緑でも**「リリース経路が検証できた」とは読まない**
#                （dist #7 の異常の想定 3・コードマスターの補足）。
#
# ${{ }} の番人が及ぶ範囲: **run: の中と、そのステップの env: の値**。
# それ以外（job や workflow レベルの env、他のステップ）は見ていない
# （検査官-solarforge-dist-15 の非ブロッキング 2。env: を写像対象に足して穴を塞いだ）。
#
# 作り: release.yml を yaml.safe_load で読み、該当ステップの run: を取り出し、
# ${{ }} を環境変数へ写して bash で走らせる。gh は PATH 上の偽物に差し替える。
# **未知の ${{ }} が残っていたら異常終了する**（式が増えた変更を黙って通さない。
# dist #7 の異常の想定 2）。
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, '..', '..'))
WORKFLOW = os.path.join(HERE, '..', 'workflows', 'release.yml')
STEP_NAME = 'Create release with rbz asset'

# ${{ ... }} → シェル変数。ここに無い式が残っていたら落とす。
EXPRESSIONS = {
    'github.event_name': '$H_EVENT_NAME',
    'github.event.inputs.version': '$H_INPUT_VERSION',
    'github.event.inputs.notes_only': '$H_INPUT_NOTES_ONLY',
}

# ステップの env: に現れてよい式。ここに無い式が増えたら落とす。
ENV_EXPRESSIONS = {'github.token'}

FAKE_GH = r'''#!/bin/sh
# 偽の gh。呼ばれた引数を $H_GH_LOG に記録し、release view だけ在/不在を返す。
echo "$@" >> "$H_GH_LOG"
if [ "$1" = "release" ] && [ "$2" = "view" ]; then
  [ "$H_RELEASE_EXISTS" = "1" ] && exit 0
  exit 1
fi
exit 0
'''


def extract_run():
    with open(WORKFLOW, encoding='utf-8') as fh:
        doc = yaml.safe_load(fh)
    steps = doc['jobs']['release']['steps']
    for step in steps:
        if step.get('name') == STEP_NAME:
            check_step_env(step.get('env') or {})
            return step['run']
    raise SystemExit(f'FATAL: ステップ "{STEP_NAME}" が release.yml に無い（名前が変わった？）')


def check_step_env(env):
    for key, value in env.items():
        for expr in re.findall(r'\$\{\{(.*?)\}\}', str(value)):
            if expr.strip() not in ENV_EXPRESSIONS:
                raise SystemExit(
                    'FATAL: ステップの env: に未知の ${{ ' + expr.strip() + ' }} がある'
                    f'（{key}）。このハーネスの ENV_EXPRESSIONS に足してから通すこと'
                    '（黙って通さないための故意の停止）'
                )


def map_expressions(script):
    def sub(match):
        expr = match.group(1).strip()
        if expr not in EXPRESSIONS:
            raise SystemExit(
                'FATAL: 未知の ${{ ' + expr + ' }} がある。'
                'このハーネスの EXPRESSIONS に写像を足してから通すこと'
                '（黙って通さないための故意の停止）'
            )
        return EXPRESSIONS[expr]

    mapped = re.sub(r'\$\{\{(.*?)\}\}', sub, script)
    if '${{' in mapped:
        raise SystemExit('FATAL: 写像しきれない ${{ }} が残っている')
    return mapped


def run_case(script, *, notes=None, notes_bytes=None, asset=None, release_exists=False,
             event_name='workflow_dispatch', version='0.3.39', notes_only='false',
             ref_name=None):
    work = tempfile.mkdtemp(prefix='sf_relgate_')
    bindir = os.path.join(work, 'bin')
    os.makedirs(bindir)
    gh = os.path.join(bindir, 'gh')
    with open(gh, 'w', encoding='utf-8') as fh:
        fh.write(FAKE_GH)
    os.chmod(gh, 0o755)
    log = os.path.join(work, 'gh.log')
    open(log, 'w').close()
    if notes_bytes is not None:
        with open(os.path.join(work, 'RELEASE_NOTES.md'), 'wb') as fh:
            fh.write(notes_bytes)
    elif notes is not None:
        with open(os.path.join(work, 'RELEASE_NOTES.md'), 'w', encoding='utf-8') as fh:
            fh.write(notes)
    if asset:
        open(os.path.join(work, asset), 'wb').close()
    env = dict(os.environ)
    env.update({
        'PATH': bindir + os.pathsep + env['PATH'],
        'H_EVENT_NAME': event_name,
        'H_INPUT_VERSION': version,
        'H_INPUT_NOTES_ONLY': notes_only,
        'H_GH_LOG': log,
        'H_RELEASE_EXISTS': '1' if release_exists else '0',
        'GITHUB_REF_NAME': ref_name or f'v{version}',
        'GITHUB_SHA': 'deadbeef',
        'GH_TOKEN': 'fake-not-a-secret',
    })
    proc = subprocess.run(['bash', '-c', script], cwd=work, env=env,
                          capture_output=True, text=True)
    with open(log, encoding='utf-8') as fh:
        calls = [line.strip() for line in fh if line.strip()]
    shutil.rmtree(work, ignore_errors=True)
    return proc.returncode, (proc.stdout + proc.stderr), calls


PASS, FAIL, UNEVAL = [], [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"{'  ok' if cond else 'FAIL'}  {name}" + (f'   {detail}' if not cond and detail else ''))


def uneval(name, why):
    """取得できなかったものは「未評価」と明示する（規程 §1）。緑の本数に混ぜない。"""
    UNEVAL.append(name)
    print(f'未評価  {name}   ({why})')


def existing_tag_first_lines():
    """既存タグの RELEASE_NOTES.md の先頭行。タグが取れなければ None を返す。

    これが要る理由: 門の枝を「歴史上どの見出し形式があるか」で決めているのに、
    その内訳を検査名の文字列として書いていたため、実測より広い主張になっていた
    （検査官-solarforge-dist-15 の必須 1。30 本が止まる状態だった）。
    **本数を名前に書く代わりに、実物のタグを門へ通す。**
    """
    try:
        res = subprocess.run(['git', 'tag'], cwd=REPO, capture_output=True, text=True)
    except OSError:
        return None
    if res.returncode != 0:
        return None
    tags = [t for t in res.stdout.split() if t.startswith('v')]
    if not tags:
        return None
    out = []
    for tag in tags:
        got = subprocess.run(['git', 'show', f'{tag}:RELEASE_NOTES.md'],
                             cwd=REPO, capture_output=True, text=True)
        if got.returncode != 0:
            out.append((tag, None))          # そのタグにファイルが無い（汎用文で公開される）
            continue
        lines = got.stdout.splitlines()
        out.append((tag, lines[0] if lines else ''))
    return out


def main():
    script = map_expressions(extract_run())
    print(f'release.yml の run: を {len(script.splitlines())} 行取り出した')

    tag = 'v0.3.39'
    good = f'# SolarForge {tag}（2026-09-25・テストユーザー向け）\n\n本文\n'
    old_style = f'# {tag} — 2026-09-25\n\n本文\n'
    # 既存 37 タグの RELEASE_NOTES.md を実測した内訳（検査官-solarforge-dist-15）。
    # 検査名にこの本数を書いておく。**名前が主張することを、この表の外に広げない。**
    h2_style = f'## SolarForge {tag}（2026-09-25）\n\n本文\n'   # 30 本（最多）
    h2_bare = f'## {tag}（2026-09-25）\n\n本文\n'               # 実例なし・同じ規則

    # ---- notes_only=true の門 ----
    rc, out, calls = run_case(script, notes_only='true')
    check('notes_only: ノートが無ければ止まる', rc == 1 and 'RELEASE_NOTES.md が無い' in out, out[:160])
    check('notes_only: ノートが無いとき gh を呼ばない', not calls, calls)

    rc, out, calls = run_case(script, notes_only='true', notes=good, release_exists=False)
    check('notes_only: Release が無ければ止まる', rc == 1 and '存在しない' in out, out[:160])
    check('notes_only: Release が無いとき edit しない',
          not any('edit' in c for c in calls), calls)

    rc, out, calls = run_case(script, notes_only='true', notes=good, release_exists=True)
    check('notes_only: 先頭行が対象タグを名乗れば本文を更新する', rc == 0, out[:160])
    check('notes_only: 更新は edit --notes-file だけ（添付・タグに触らない）',
          any('release edit' in c and '--notes-file' in c for c in calls)
          and not any('upload' in c or 'create' in c for c in calls), calls)

    rc, out, _ = run_case(script, notes_only='true', release_exists=True,
                          notes=f'# SolarForge {tag}\n')
    check('notes_only: 先頭行がタグで終わっていても通る（行末）', rc == 0, out[:160])

    # 見出しの 4 形式。既存 37 タグの実測内訳を名前に書く（dist #7 の 2 の差し戻し）。
    rc, out, _ = run_case(script, notes_only='true', release_exists=True, notes=old_style)
    check('notes_only: `# <タグ>` 形式が通る（既存 1 本・v0.3.37）', rc == 0, out[:160])

    rc, out, _ = run_case(script, notes_only='true', release_exists=True, notes=h2_style)
    check('★notes_only: `## SolarForge <タグ>` 形式が通る（既存 30 本・歴史上いちばん多い）',
          rc == 0, out[:160])

    rc, out, _ = run_case(script, notes_only='true', release_exists=True, notes=h2_bare)
    check('notes_only: `## <タグ>` 形式も通る（既存に実例なし・同じ規則の範囲）',
          rc == 0, out[:160])

    rc, out, _ = run_case(script, notes_only='true', release_exists=True,
                          version='0.3.3', notes=f'## SolarForge {tag}（…）\n')
    check('★notes_only: `##` でも接頭辞の取り違えを弾く（version=0.3.3 が v0.3.39 を通さない）',
          rc == 1, out[:160])

    rc, out, _ = run_case(script, notes_only='true', release_exists=True,
                          notes=f'## SolarForge {tag}-rc1（…）\n')
    check('★notes_only: `##` でも接尾辞 -rc1 を弾く', rc == 1, out[:160])

    rc, out, _ = run_case(script, notes_only='true', release_exists=True,
                          notes=f'### SolarForge {tag}（…）\n')
    check('notes_only: `###`（レベル 3）は止まる（既存に無い形は通さない）', rc == 1, out[:160])

    rc, out, calls = run_case(script, notes_only='true', release_exists=True, notes='')
    check('notes_only: 空のノートは止まる', rc == 1, out[:160])
    check('notes_only: 空のとき edit しない', not any('edit' in c for c in calls), calls)

    rc, out, _ = run_case(script, notes_only='true', release_exists=True,
                          notes='# なにか別のファイル\n')
    check('notes_only: 別ファイルの見出しは止まる', rc == 1, out[:160])

    rc, out, _ = run_case(script, notes_only='true', release_exists=True,
                          version='0.3.3', notes=f'# SolarForge {tag}（…）\n')
    check('★notes_only: 接頭辞の取り違えを弾く（version=0.3.3 が v0.3.39 のノートを通さない）',
          rc == 1, out[:160])

    # dist #7 の 3: 接尾辞
    for suffix in ('-rc1', '+build', '_2'):
        rc, out, _ = run_case(script, notes_only='true', release_exists=True,
                              notes=f'# SolarForge {tag}{suffix}（…）\n')
        check(f'★notes_only: 接尾辞 {suffix} を弾く（タグは {tag}）', rc == 1, out[:160])

    # dist #7 の 4: BOM
    rc, out, _ = run_case(script, notes_only='true', release_exists=True,
                          notes_bytes=('﻿' + good).encode('utf-8'))
    check('notes_only: BOM 付きは止まる（fail-closed）', rc == 1, out[:160])
    check('★notes_only: BOM 付きのエラーが BOM に触れている', 'BOM' in out, out[:200])

    # ---- 既定経路（タグ push / notes_only=false）----
    asset = f'SolarForge-0.3.39.rbz'
    rc, out, calls = run_case(script, event_name='push', notes=good, asset=asset)
    check('既定経路: 先頭行が対象タグを名乗れば公開する', rc == 0, out[:160])
    check('既定経路: create に本文とアセットが渡る',
          any('release create' in c and '--notes-file' in c and asset in c for c in calls), calls)

    rc, out, calls = run_case(script, event_name='push', notes=old_style, asset=asset)
    check('既定経路: `# <タグ>` 形式が通る（既存 1 本・v0.3.37）', rc == 0, out[:160])

    rc, out, calls = run_case(script, event_name='push', notes=h2_style, asset=asset)
    check('★既定経路: `## SolarForge <タグ>` 形式が通る（既存 30 本・歴史上いちばん多い）',
          rc == 0, out[:160])

    rc, out, calls = run_case(script, event_name='push', asset=asset,
                              notes='# SolarForge v0.3.30（古い版のまま）\n')
    check('★既定経路: 先頭行が別版なら止まる（dist #7 の 2）', rc == 1, out[:160])
    check('★既定経路: 別版のとき create も upload もしない',
          not any('create' in c or 'upload' in c for c in calls), calls)

    rc, out, calls = run_case(script, event_name='push', notes=good, asset=asset,
                              release_exists=True)
    check('既定経路: 既存 Release なら upload --clobber と edit',
          rc == 0 and any('upload' in c and '--clobber' in c for c in calls)
          and any('release edit' in c for c in calls), calls)

    rc, out, calls = run_case(script, event_name='push', notes=good)
    check('既定経路: アセットが無ければ止まる', rc == 1 and 'not found' in out, out[:160])

    rc, out, calls = run_case(script, event_name='push', asset=asset)
    check('既定経路: ノートが無ければ汎用文で公開する（門を掛けない）',
          rc == 0 and any('release create' in c and '--notes ' in c for c in calls), calls)

    # ---- 既定経路を workflow_dispatch で（実際にリリースを打っているのはこちら）----
    # 検査官-solarforge-dist-15 の非ブロッキング 1: 既定経路の検査が全部 push だったので、
    # dispatch 側だけが持つ `EXTRA=--target ${GITHUB_SHA}` を消す変異が素通しだった。
    # release.yml の実行 38 回はすべて workflow_dispatch（タグ push は 0 回）。
    rc, out, calls = run_case(script, event_name='workflow_dispatch', notes_only='false',
                              notes=good, asset=asset)
    check('既定経路(workflow_dispatch): 先頭行が対象タグを名乗れば公開する', rc == 0, out[:160])
    check('★既定経路(workflow_dispatch): create に --target <SHA> が渡る',
          any('release create' in c and '--target deadbeef' in c for c in calls), calls)

    rc, out, calls = run_case(script, event_name='push', notes=good, asset=asset)
    check('★既定経路(タグ push): create に --target を渡さない（タグが指す先を動かさない）',
          any('release create' in c for c in calls)
          and not any('--target' in c for c in calls), calls)

    rc, out, calls = run_case(script, event_name='workflow_dispatch', notes_only='false',
                              asset=asset, notes='# SolarForge v0.3.30（古い版のまま）\n')
    check('★既定経路(workflow_dispatch): 先頭行が別版なら止まる', rc == 1, out[:160])
    check('★既定経路(workflow_dispatch): 別版のとき create も upload もしない',
          not any('create' in c or 'upload' in c for c in calls), calls)

    rc, out, calls = run_case(script, event_name='workflow_dispatch', notes_only='false',
                              notes=h2_style, asset=asset, release_exists=True)
    check('既定経路(workflow_dispatch): 既存 Release なら upload --clobber と edit',
          rc == 0 and any('upload' in c and '--clobber' in c for c in calls)
          and any('release edit' in c for c in calls), calls)

    # ---- 既存タグの実物を門へ通す（必須 1 の再発防止）----
    rows = existing_tag_first_lines()
    if rows is None:
        uneval('既存タグの RELEASE_NOTES.md がすべて門を通る',
               'git のタグが取れない（浅いチェックアウト等）。'
               'CI では actions/checkout に fetch-tags: true を付けてある')
    else:
        stopped, passed, nofile = [], 0, 0
        for tag, line in rows:
            if line is None:
                nofile += 1
                continue
            rc, _, _ = run_case(script, notes_only='true', release_exists=True,
                                version=tag[1:], notes=line + '\n')
            if rc == 0:
                passed += 1
            else:
                stopped.append((tag, line[:40]))
        check(f'★既存タグ {len(rows)} 本の RELEASE_NOTES.md がすべて門を通る'
              f'（通った {passed} / 止まった {len(stopped)} / ファイル無し {nofile}）',
              not stopped, stopped[:6])

    print(f'\n{len(PASS)} passed / {len(FAIL)} failed'
          + (f' / {len(UNEVAL)} 未評価' if UNEVAL else ''))
    if FAIL:
        for name in FAIL:
            print(f'  FAILED: {name}')
        sys.exit(1)


if __name__ == '__main__':
    main()

"""Verify the initial Pinot fixes before publishing an upstream-integrated build."""

import os
from pathlib import Path
import subprocess
import tempfile


SNAPSHOT = 'c969c9cb013b40842db5e98222912b5b623670a7'
INITIAL_PATCHES = [
    '22a208c9b40252d8ade2e547651e72f12e28dee4',
    '9339800eeb0da3c9b4fce14257e3cd9f2406f995',
    '40e624c2cf7b29ae42f2333f602bdb791d7279a6',
    '8787edeb112fcbeef3fd8aa182d159b9b251ddb4',
]
PRODUCTION = ['plugin/trino-pinot/src/main', 'plugin/trino-pinot/pom.xml']


def run(*args, input=None, env=None, check=True):
    return subprocess.run(args, input=input, env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def git(*args):
    return run('git', *args).stdout.strip()


def patch_id(diff):
    result = run('git', 'patch-id', '--stable', input=diff).stdout.split()
    if not result:
        raise ValueError('Required initial Pinot production patch is empty')
    return result[0]


def verify_integrated_patches(target):
    """Return history and current-source evidence, without changing the checkout.

    Individual upstream commits or one squash of all four fixes are recognized.
    Historical patches in Git history are insufficient alone: a reverse check
    also proves their complete production diff is still present in the target.
    Unfamiliar integration shapes require review instead of publishing silently.
    """
    if run('git', 'cat-file', '-e', f'{SNAPSHOT}^{{commit}}', check=False).returncode:
        run('git', 'fetch', '--no-tags', 'origin', f'refs/tags/pinot-dev-snapshot/{SNAPSHOT}')
        if git('rev-parse', 'FETCH_HEAD^{commit}') != SNAPSHOT:
            raise ValueError('The initial immutable Pinot snapshot differs')
    for commit in INITIAL_PATCHES:
        run('git', 'merge-base', '--is-ancestor', commit, SNAPSHOT)
    target_sha = git('rev-parse', f'{target}^{{commit}}')
    required = [{'source_sha': commit,
                 'patch_id': patch_id(run('git', 'diff', f'{commit}^', commit, '--', *PRODUCTION).stdout)}
                for commit in INITIAL_PATCHES]
    combined = run('git', 'diff', f'{INITIAL_PATCHES[0]}^', INITIAL_PATCHES[-1], '--', *PRODUCTION).stdout
    combined_id = patch_id(combined)
    history = run('git', 'log', '--no-merges', '--pretty=format:commit %H', '-p', target_sha, '--', *PRODUCTION).stdout
    equivalents = {parts[0]: parts[1] for line in run('git', 'patch-id', '--stable', input=history).stdout.splitlines()
                   if len(parts := line.split()) == 2}
    if all(patch['patch_id'] in equivalents for patch in required):
        evidence = 'individual-commits'
        for patch in required:
            patch['upstream_sha'] = equivalents[patch['patch_id']]
        integration_shas = sorted({patch['upstream_sha'] for patch in required})
    elif combined_id in equivalents:
        evidence = 'combined-squash'
        integration_shas = [equivalents[combined_id]]
    else:
        raise ValueError('Target history does not contain all four reviewed initial Pinot fixes; manual review is required')
    with tempfile.TemporaryDirectory(prefix='pinot-upstream-proof-') as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory, 'index')))
        run('git', 'read-tree', target_sha, env=env)
        checked = run('git', 'apply', '--reverse', '--cached', '--check', input=combined, env=env, check=False)
        if checked.returncode:
            raise ValueError('Initial Pinot fixes were reverted or changed in the target; manual review is required')
    return {'snapshot_sha': SNAPSHOT, 'target_sha': target_sha,
            'history_evidence': evidence, 'integration_commits': integration_shas, 'initial_patches': required,
            'combined_production_patch_id': combined_id, 'reverse_production_check': 'passed'}

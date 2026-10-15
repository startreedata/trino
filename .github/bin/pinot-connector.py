#!/usr/bin/env python3
"""Replay the complete Pinot development stack against a released Trino version."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.request import urlopen
import xml.etree.ElementTree as ET


UPSTREAM = 'https://github.com/trinodb/trino.git'
MANIFEST = '.github/pinot-release.json'
PINOT_DOC = 'docs/src/main/sphinx/connector/pinot.md'
PATCH_PATHS = ['plugin/trino-pinot', PINOT_DOC]
BOT = 'github-actions[bot]'
MIN_TRINO_VERSION = 429


def run(*args, input=None, check=True):
    return subprocess.run(args, input=input, text=True, stdout=subprocess.PIPE, check=check)


def git(*args):
    return run('git', *args).stdout.strip()


def output(**values):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        for key, value in values.items():
            stream.write(f'{key}={value}\n')


def fetch(remote, ref):
    git('fetch', '--no-tags', remote, ref)
    return git('rev-parse', 'FETCH_HEAD^{commit}')


def positive(value):
    if not re.fullmatch(r'[1-9][0-9]*', value):
        raise ValueError(f'Expected a positive integer: {value}')
    return int(value)


def configure_git():
    git('config', 'user.name', BOT)
    git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')


def read_manifest(ref):
    return json.loads(git('show', f'{ref}:{MANIFEST}'))


def select(args):
    releases = run('gh', 'api', '--paginate', 'repos/trinodb/trino/releases',
                   '--jq', '.[] | select(.draft == false and .prerelease == false) | .tag_name').stdout
    versions = sorted({int(tag) for tag in releases.splitlines() if re.fullmatch(r'[1-9][0-9]*', tag)})
    if not versions:
        raise ValueError('No published stable upstream Trino releases were found')
    # Older Trino releases predate the GitHub release catalog. Keep the modern
    # published release ceiling while including the historical Central catalog.
    with urlopen('https://repo1.maven.org/maven2/io/trino/trino-pinot/maven-metadata.xml', timeout=30) as response:
        central = ET.fromstring(response.read())
    historical = {int(item.text) for item in central.findall('versioning/versions/version')
                  if item.text.isdigit() and int(item.text) <= versions[-1]}
    versions = sorted(set(versions) | historical)
    current = fetch('origin', 'refs/heads/pinot-dev')
    base = ''
    expected = ''
    if args.mode == 'tag' or (args.mode == 'ci' and args.ref.startswith('startree/pinot-')):
        manifest = read_manifest(args.source)
        current, base = manifest['dev_sha'], manifest['dev_base_sha']
        expected = args.source
        selected = [positive(manifest['trino_version'])]
        patch = manifest['release'].split('-startree.')[1]
        if args.mode == 'tag' and args.ref != manifest['release']:
            raise ValueError('Tag name does not match its release manifest')
    else:
        patch = str(positive(args.patch_number))
        reserved = git('ls-remote', '--tags', 'origin')
        if args.dev_snapshot:
            if not re.fullmatch(r'pinot-dev-snapshot/[0-9a-f]{40}', args.dev_snapshot):
                raise ValueError('Expected an immutable pinot-dev-snapshot/<full-sha> tag')
            fetch('origin', f'refs/tags/{args.dev_snapshot}')
            snapshot = json.loads(git('cat-file', 'tag', 'FETCH_HEAD').split('\n\n', 1)[1])
            current, base = snapshot['dev_sha'], snapshot['dev_base_sha']
            if git('rev-parse', 'FETCH_HEAD^{commit}') != current:
                raise ValueError('Snapshot tag does not match its recorded dev SHA')
        elif args.mode == 'ci':
            current = args.source
        if args.trino_version == 'all-missing':
            releases = run('gh', 'api', '--paginate', f'repos/{os.environ["GITHUB_REPOSITORY"]}/releases',
                           '--jq', '.[] | select(.draft == false) | .tag_name').stdout
            published = {int(match) for match in re.findall(r'^([1-9][0-9]*)-startree\.[1-9][0-9]*$', releases, re.M)}
            selected = [version for version in versions if version >= MIN_TRINO_VERSION and version not in published]
        else:
            selected = [versions[-1] if args.trino_version == 'latest' else positive(args.trino_version)]
    matrix = []
    for version in selected:
        number = patch
        if args.trino_version == 'all-missing':
            taken = [int(value) for value in re.findall(rf'refs/tags/{version}-startree\.([1-9][0-9]*)(?:\^\{{\}})?$', reserved, re.M)]
            number = str(max(taken, default=0) + 1)
        matrix.append({'trino': str(version), 'patch': number, 'dev_sha': current,
                       'release': f'{version}-startree.{number}' if args.mode != 'ci' or expected else f'{version}-startree-SNAPSHOT',
                       'base_sha': base, 'expected_source': expected})
    if any(version < MIN_TRINO_VERSION or version not in versions for version in selected):
        raise ValueError(f'Only published upstream Trino versions >= {MIN_TRINO_VERSION} are supported')
    output(matrix=json.dumps(matrix, separators=(',', ':')), count=len(matrix))


def allowed(path):
    return (path.startswith('plugin/trino-pinot/') or path in ['README.md', PINOT_DOC]
            or path.startswith('.github/workflows/')
            or path.startswith('.github/bin/pinot-connector') or path.startswith('.github/pinot-'))


def patch_ids(ref):
    history = run('git', 'log', '--no-merges', '--pretty=format:%H', '-p', ref, '--', *PATCH_PATHS).stdout
    return {line.split()[0] for line in run('git', 'patch-id', '--stable', input=history).stdout.splitlines()}


def prepare(args):
    git('diff', '--exit-code', 'HEAD')
    positive(args.trino_version)
    if args.release != f'{args.trino_version}-startree-SNAPSHOT':
        if not re.fullmatch(rf'{args.trino_version}-startree\.[1-9][0-9]*', args.release):
            raise ValueError('Release must be N-startree.K and match the upstream version')
    dev = git('rev-parse', f'{args.dev_sha}^{{commit}}')
    adapter_path = Path(__file__).with_name('pinot-connector-compat.py')
    adapter_sha = hashlib.sha256(adapter_path.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location('pinot_connector_compat', adapter_path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    adaptations = []
    master = fetch(UPSTREAM, 'refs/heads/master')
    base = args.base_sha or git('merge-base', dev, master)
    git('merge-base', '--is-ancestor', base, dev)
    git('merge-base', '--is-ancestor', base, master)
    commits = git('rev-list', '--reverse', '--topo-order', f'{base}..{dev}').splitlines()
    if git('rev-list', '--merges', f'{base}..{dev}'):
        raise ValueError('Dev-only merge commits are unsupported; keep pinot-dev rebased')
    for commit in commits:
        paths = git('diff-tree', '--no-commit-id', '--name-only', '-r', commit).splitlines()
        if not paths or any(not allowed(path) for path in paths):
            raise ValueError(f'Unsupported changes in downstream commit {commit}: {paths}')
    upstream = fetch(UPSTREAM, f'refs/tags/{args.trino_version}')
    equivalent = patch_ids(upstream)
    configure_git()
    git('checkout', '--detach', upstream)
    records = []
    for commit in commits:
        patch = run('git', 'diff', f'{commit}^', commit, '--', *PATCH_PATHS).stdout
        record = {'sha': commit, 'subject': git('show', '-s', '--format=%s', commit)}
        if not patch:
            record['status'] = 'tooling-copied-from-dev-snapshot'
        else:
            patch_id = run('git', 'patch-id', '--stable', input=patch).stdout.split()[0]
            if patch_id in equivalent:
                record['status'] = 'already-upstream'
            else:
                applied = run('git', 'apply', '--3way', '--index', input=patch, check=False)
                adapted = applied.returncode != 0
                if adapted:
                    adaptations.extend(adapter.resolve_conflicts(commit, int(args.trino_version)))
                if not git('diff', '--cached', '--name-only'):
                    record['status'] = 'already-present-empty-patch'
                else:
                    message = git('show', '-s', '--format=%B', commit)
                    run('git', 'commit', '--author', git('show', '-s', '--format=%an <%ae>', commit),
                        '--date', git('show', '-s', '--format=%aI', commit), '-F', '-',
                        input=f'{message}\n\n(cherry picked from commit {commit})\n')
                    record['status'] = 'applied-with-compatibility' if adapted else 'applied'
                    equivalent.add(patch_id)
        records.append(record)
    adaptations.extend(adapter.normalize(int(args.trino_version)))
    if git('diff', '--name-only', 'HEAD'):
        git('add', '--sparse', '--all', *PATCH_PATHS)
        git('commit', '-m', f'Adapt the complete Pinot patch stack to Trino {args.trino_version}')
    # Copy automation from the pinned snapshot, including intentional removal of upstream workflows.
    shutil.rmtree('.github/workflows')
    paths = git('ls-tree', '-r', '--name-only', dev, '--', '.github').splitlines()
    for path in paths:
        if not (path.startswith('.github/workflows/') or path.startswith('.github/bin/pinot-connector') or path.startswith('.github/pinot-')):
            continue
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(subprocess.check_output(['git', 'show', f'{dev}:{path}']))
    Path('README.md').write_bytes(subprocess.check_output(['git', 'show', f'{dev}:README.md']))
    integrated = None
    if not args.release.endswith('-SNAPSHOT') and not git('diff', '--name-only', upstream, 'HEAD', '--', 'plugin/trino-pinot/src/main'):
        guard_path = Path(__file__).with_name('pinot-connector-upstream.py')
        spec = importlib.util.spec_from_file_location('pinot_connector_upstream', guard_path)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        integrated = guard.verify_integrated_patches(upstream)
        integrated['guard_sha256'] = hashlib.sha256(guard_path.read_bytes()).hexdigest()
    manifest = {'schema': 1, 'release': args.release, 'trino_version': args.trino_version,
                'dev_sha': dev, 'dev_base_sha': base, 'upstream_master_sha': master,
                'upstream_sha': upstream, 'patches': records,
                'compatibility': {'adapter_sha256': adapter_sha, 'adaptations': adaptations},
                'known_limitations': [paragraph.replace('\n', ' ') for paragraph in Path('README.md').read_text().split('\n\n')
                                      if paragraph.startswith('Known limitation')]}
    if integrated:
        manifest['upstream_incorporation'] = integrated
    # Historical rebuilds retain the original upstream-master observation as well as the source base.
    if args.expected_source:
        original = read_manifest(args.expected_source)
        manifest['upstream_master_sha'] = original['upstream_master_sha']
    Path(MANIFEST).write_text(json.dumps(manifest, indent=2) + '\n')
    git('add', '--all', '.github', 'README.md')
    git('commit', '-m', f'Prepare Pinot connector {args.release} from full dev stack')
    source = git('rev-parse', 'HEAD')
    if args.expected_source:
        git('diff', '--exit-code', args.expected_source, source)
        git('checkout', '--detach', args.expected_source)
        source = git('rev-parse', 'HEAD')
    ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
    pom = ET.parse('pom.xml')
    if pom.findtext('m:version', namespaces=ns) != args.trino_version:
        raise ValueError('Release POM version differs from target Trino release')
    java = pom.findtext('m:properties/m:temurin.release', namespaces=ns)
    if java:
        java = java.removeprefix('jdk-').split('+')[0]
    else:
        java = pom.findtext('m:properties/m:project.build.targetJdk', namespaces=ns)
    if not java:
        raise ValueError('Cannot determine the upstream release JDK')
    output(java=java, source_sha=source)


def assets(args):
    metadata = read_manifest('HEAD')
    metadata.update(source_repository=os.environ['GITHUB_REPOSITORY'], source_sha=git('rev-parse', 'HEAD'),
                    workflow_run=f'https://github.com/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{os.environ["GITHUB_RUN_ID"]}')
    Path('dist').mkdir()
    name = f'trino-pinot-{metadata["release"]}'
    archive = Path(f'dist/{name}.zip')
    shutil.copyfile(f'plugin/trino-pinot/target/trino-pinot-{metadata["trino_version"]}.zip', archive)
    Path(f'{archive}.sha256').write_text(f'{hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()}  {archive.name}\n')
    Path(f'dist/{name}.json').write_text(json.dumps(metadata, indent=2) + '\n')
    git('update-ref', 'refs/pinot/prepared-release', 'HEAD')
    git('bundle', 'create', 'source.bundle', 'refs/pinot/prepared-release', f'^{metadata["upstream_sha"]}')


def publish(args):
    metadata = json.loads(next(Path(args.artifact, 'dist').glob('*.json')).read_text())
    release = metadata['release']
    if not re.fullmatch(r'[1-9][0-9]*-startree\.[1-9][0-9]*', release):
        raise ValueError('Invalid release version')
    if run('gh', 'release', 'view', release, check=False).returncode == 0:
        raise ValueError('Release already exists; releases and drafts are never replaced')
    archive = Path(args.artifact, 'dist', f'trino-pinot-{release}.zip')
    if Path(f'{archive}.sha256').read_text().split()[0] != hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest():
        raise ValueError('Connector archive checksum mismatch')
    configure_git()
    fetch('origin', metadata['dev_sha'])
    git('fetch', str(Path(args.artifact, 'source.bundle').resolve()), 'refs/pinot/prepared-release')
    if git('rev-parse', 'FETCH_HEAD') != metadata['source_sha']:
        raise ValueError('Verified source bundle differs from build provenance')
    if read_manifest('FETCH_HEAD') != {key: value for key, value in metadata.items()
                                     if key not in ['source_repository', 'source_sha', 'workflow_run']}:
        raise ValueError('Source manifest differs from build provenance')
    snapshot = f'pinot-dev-snapshot/{metadata["dev_sha"]}'
    existing = git('ls-remote', '--tags', 'origin', f'refs/tags/{snapshot}')
    if existing:
        snapshot_sha = fetch('origin', f'refs/tags/{snapshot}')
        payload = json.loads(git('cat-file', 'tag', 'FETCH_HEAD').split('\n\n', 1)[1])
        if snapshot_sha != metadata['dev_sha'] or payload != {
                'dev_sha': metadata['dev_sha'], 'dev_base_sha': metadata['dev_base_sha']}:
            raise ValueError('Existing immutable dev snapshot differs')
    else:
        git('tag', '-a', snapshot, metadata['dev_sha'], '-m', json.dumps({
            'dev_sha': metadata['dev_sha'], 'dev_base_sha': metadata['dev_base_sha']}))
        # Backfill jobs share one dev snapshot. Another verified job may create
        # the same immutable tag concurrently, so verify the winner on failure.
        created = run('git', 'push', 'origin', f'refs/tags/{snapshot}', check=False)
        if created.returncode:
            snapshot_sha = fetch('origin', f'refs/tags/{snapshot}')
            payload = json.loads(git('cat-file', 'tag', 'FETCH_HEAD').split('\n\n', 1)[1])
            if snapshot_sha != metadata['dev_sha'] or payload != {
                    'dev_sha': metadata['dev_sha'], 'dev_base_sha': metadata['dev_base_sha']}:
                raise ValueError('Concurrent immutable dev snapshot differs')
    existing_release = git('ls-remote', '--tags', 'origin', f'refs/tags/{release}')
    if existing_release:
        if not args.existing_tag or fetch('origin', f'refs/tags/{release}') != metadata['source_sha']:
            raise ValueError('Release tag already exists; published artifacts are never replaced')
    branch = f'refs/heads/startree/pinot-{metadata["trino_version"]}'
    observed = git('ls-remote', 'origin', branch)
    lease = observed.split()[0] if observed else ''
    if not existing_release:
        git('tag', release, metadata['source_sha'])
    refspecs = [f'refs/tags/{release}']
    if lease:
        fetch('origin', branch)
        previous = run('git', 'show', f'{lease}:{MANIFEST}', check=False)
        previous_patch = int(json.loads(previous.stdout)['release'].split('-startree.')[1]) if previous.returncode == 0 else 0
    else:
        previous_patch = 0
    if previous_patch <= int(release.split('-startree.')[1]):
        refspecs.append(f'{metadata["source_sha"]}:{branch}')
    git('push', '--atomic', f'--force-with-lease={branch}:{lease}', 'origin', *refspecs)
    patches = '\n'.join(f'- {item["subject"]} ({item["sha"][:12]}): {item["status"]}' for item in metadata['patches'])
    notes = Path(args.artifact, 'release-notes.md')
    notes.write_text(f'Pinot connector only, for Trino {metadata["trino_version"]}. See README for installation.\n\n'
                     f'Complete dev snapshot: `{metadata["dev_sha"]}`\n\n{patches}\n\n'
                     + '\n\n'.join(metadata['known_limitations']) + '\n')
    run('gh', 'release', 'create', release, '--draft', '--verify-tag', '--title', f'Pinot connector {release}', '--notes-file', str(notes))
    run('gh', 'release', 'upload', release, *map(str, sorted(Path(args.artifact, 'dist').iterdir())))
    run('gh', 'release', 'edit', release, '--draft=false', '--latest=false')


def sync(args):
    observed = fetch('origin', 'refs/heads/pinot-dev')
    fork_master = fetch('origin', 'refs/heads/master')
    upstream = fetch(UPSTREAM, 'refs/heads/master')
    base = git('merge-base', observed, upstream)
    if git('rev-list', '--merges', f'{base}..{observed}'):
        raise ValueError('Refusing to flatten dev-only merge commits during upstream sync')
    deleted = set(git('diff', '--name-only', '--diff-filter=D', base, observed, '--', '.github/workflows').splitlines())
    configure_git()
    git('checkout', '-B', 'pinot-dev', observed)
    result = run('git', 'rebase', '--onto', upstream, base, check=False)
    while result.returncode:
        conflicts = git('diff', '--name-only', '--diff-filter=U').splitlines()
        if not conflicts or any(path not in deleted for path in conflicts):
            git('rebase', '--abort')
            raise ValueError(f'Upstream sync requires manual conflict resolution: {conflicts}')
        for path in conflicts:
            # Resolve only modify/delete conflicts where the downstream intentionally deleted the workflow.
            stages = git('ls-files', '--unmerged', '--', path)
            if any(line.split()[2] == '3' for line in stages.splitlines()):
                git('rebase', '--abort')
                raise ValueError(f'Unexpected non-deletion workflow conflict: {path}')
            git('rm', '--', path)
        result = run('git', '-c', 'core.editor=true', 'rebase', '--continue', check=False)
    inherited = [path for path in Path('.github/workflows').glob('*')
                 if path.name not in ['pinot-connector.yml', 'pinot-connector-build.yml']]
    for path in inherited:
        git('rm', '--', str(path))
    if inherited:
        git('commit', '-m', 'Keep inherited upstream workflows disabled in the Pinot fork')
    git('merge-base', '--is-ancestor', fork_master, upstream)
    dev = git('rev-parse', 'HEAD')
    snapshot = f'pinot-dev-snapshot/{dev}'
    branch = f'refs/heads/startree/dev-snapshots/{dev}'
    payload = {'dev_sha': dev, 'dev_base_sha': upstream}
    refspecs = [f'{upstream}:refs/heads/master', 'HEAD:refs/heads/pinot-dev']
    leases = [f'--force-with-lease=refs/heads/pinot-dev:{observed}']
    retained = git('ls-remote', 'origin', branch)
    if retained and retained.split()[0] != dev:
        raise ValueError('Existing retained development branch differs')
    if not retained:
        leases.append(f'--force-with-lease={branch}:')
        refspecs.append(f'{dev}:{branch}')
    if git('ls-remote', '--tags', 'origin', f'refs/tags/{snapshot}'):
        if fetch('origin', f'refs/tags/{snapshot}') != dev or json.loads(
                git('cat-file', 'tag', 'FETCH_HEAD').split('\n\n', 1)[1]) != payload:
            raise ValueError('Existing immutable development snapshot differs')
    else:
        git('tag', '-a', snapshot, dev, '-m', json.dumps(payload))
        refspecs.append(f'refs/tags/{snapshot}')
    # Keep identical workflow files on a branch so GITHUB_TOKEN can publish
    # older verified snapshots after pinot-dev moves, without workflow-write scope.
    git('push', '--atomic', *leases, 'origin', *refspecs)
    output(dev_snapshot=snapshot)


parser = argparse.ArgumentParser(description=__doc__)
commands = parser.add_subparsers(dest='command', required=True)
selector = commands.add_parser('select')
selector.add_argument('--mode', choices=['ci', 'release', 'tag'], required=True)
selector.add_argument('--source', required=True)
selector.add_argument('--ref', default='pinot-dev')
selector.add_argument('--trino-version', default='latest')
selector.add_argument('--patch-number', default='1')
selector.add_argument('--dev-snapshot', default='')
preparer = commands.add_parser('prepare')
preparer.add_argument('--dev-sha', required=True)
preparer.add_argument('--base-sha', default='')
preparer.add_argument('--trino-version', required=True)
preparer.add_argument('--release', required=True)
preparer.add_argument('--expected-source', default='')
commands.add_parser('assets')
publisher = commands.add_parser('publish')
publisher.add_argument('--artifact', required=True)
publisher.add_argument('--existing-tag', action='store_true')
commands.add_parser('sync')
arguments = parser.parse_args()
globals()[arguments.command](arguments)

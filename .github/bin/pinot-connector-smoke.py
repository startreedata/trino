#!/usr/bin/env python3
"""Load the connector in a checksum-verified released Trino server."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request


CENTRAL = 'https://repo.maven.apache.org/maven2'
CACHE = 'https://maven-central.storage-download.googleapis.com/maven2'


def request_json(url, sql=None):
    request = urllib.request.Request(url, data=sql.encode() if sql is not None else None,
                                     headers={'X-Trino-User': 'pinot-smoke', 'Content-Type': 'text/plain'})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def query(base, sql):
    result = request_json(base + '/v1/statement', sql)
    rows = []
    deadline = time.monotonic() + 60
    while True:
        if 'error' in result:
            raise RuntimeError(f'{sql}: {result["error"]}')
        rows.extend(result.get('data', []))
        if 'nextUri' not in result:
            return rows
        if time.monotonic() > deadline:
            raise TimeoutError(f'Trino query timed out: {sql}')
        result = request_json(result['nextUri'])


def download_server(version, work):
    artifact = f'io/trino/trino-server/{version}/trino-server-{version}.tar.gz'
    checksum_url = f'{CENTRAL}/{artifact}.sha1'
    with urllib.request.urlopen(checksum_url, timeout=30) as response:
        checksum = response.read(4096).decode().strip().split()[0].lower()
    if not re.fullmatch('[0-9a-f]{40}', checksum):
        raise ValueError('Invalid checksum from the primary Maven Central repository')
    archive = work / f'trino-server-{version}.tar.gz'
    for repository in [CENTRAL, CACHE]:
        url = f'{repository}/{artifact}'
        try:
            digest = hashlib.sha1()
            with urllib.request.urlopen(url, timeout=60) as response, archive.open('wb') as stream:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    stream.write(chunk)
            break
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if repository == CACHE:
                raise RuntimeError('Released stock Trino server could not be downloaded') from error
            print(f'Primary server download unavailable; trying the checksum-verified Central cache: {error}', flush=True)
    if digest.hexdigest() != checksum:
        raise ValueError('Released stock Trino server checksum mismatch')
    print(f'Verified stock Trino {version}: {url}; SHA-1 {checksum} from {checksum_url}', flush=True)
    with tarfile.open(archive) as distribution:
        distribution.extractall(work, filter='data')
    archive.unlink()
    return work / f'trino-server-{version}', {'server_url': url, 'checksum_url': checksum_url, 'sha1': checksum}


def smoke(args):
    version = str(args.version)
    if not re.fullmatch('[1-9][0-9]*', version) or int(version) < 429:
        raise ValueError('Expected a Trino release number >= 429')
    plugin = Path(args.plugin).resolve()
    if not (plugin / f'trino-pinot-{version}.jar').is_file():
        raise ValueError('The complete packaged Pinot plugin must match the requested Trino version')
    manifest = json.loads(Path('.github/pinot-release.json').read_text())
    upstream = manifest['upstream_sha']
    if str(manifest['trino_version']) != version or not re.fullmatch('[0-9a-f]{40}', upstream):
        raise ValueError('The release manifest does not match the smoke-test target')
    jvm = subprocess.check_output(['git', 'show', f'{upstream}:core/docker/default/etc/jvm.config'], text=True)
    # Docker's native kill agent and percentage heap sizing do not apply to the host process.
    flags = [line for line in jvm.splitlines() if not line.startswith((
        '-agentpath:/usr/lib/trino/bin/libjvmkill.so', '-XX:InitialRAMPercentage=',
        '-XX:MaxRAMPercentage=', '-Xms', '-Xmx'))]
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=False)
    server, evidence = download_server(version, work)
    shutil.rmtree(server / 'plugin/pinot')
    shutil.copytree(plugin, server / 'plugin/pinot')
    etc = server / 'etc'
    (etc / 'catalog').mkdir(parents=True, exist_ok=True)
    with socket.socket() as port_socket:
        port_socket.bind(('127.0.0.1', 0))
        port = port_socket.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    (etc / 'jvm.config').write_text('\n'.join(flags + ['-Xms256M', '-Xmx2G']) + '\n')
    (etc / 'node.properties').write_text(f'node.environment=testing\nnode.id=pinot-smoke\nnode.internal-address=127.0.0.1\nnode.data-dir={work / "data"}\n')
    (etc / 'config.properties').write_text(f'coordinator=true\nnode-scheduler.include-coordinator=true\nhttp-server.http.port={port}\ndiscovery.uri={base}\nquery.max-memory-per-node=256MB\nmemory.heap-headroom-per-node=512MB\n')
    (etc / 'catalog/pinot.properties').write_text('connector.name=pinot\npinot.controller-urls=http://127.0.0.1:8098\n')
    log = work / 'server.log'
    with log.open('w') as stream:
        process = subprocess.Popen([str(server / 'bin/launcher'), 'run'], cwd=server,
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 240
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f'Stock Trino exited with status {process.returncode}')
                if time.monotonic() > deadline:
                    raise TimeoutError('Stock Trino did not become ready')
                try:
                    if not request_json(base + '/v1/info').get('starting', False):
                        break
                except (urllib.error.URLError, TimeoutError):
                    pass
                time.sleep(1)
            engine = query(base, 'SELECT version()')
            catalogs = query(base, 'SHOW CATALOGS')
            if engine != [[version]] or ['pinot'] not in catalogs:
                raise RuntimeError(f'Wrong stock Trino version or missing Pinot catalog: {engine}, {catalogs}')
            evidence.update(trino_version=version, version_query=engine, catalogs=catalogs, result='passed')
            print(json.dumps(evidence, sort_keys=True), flush=True)
            if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(summary, 'a') as output:
                    output.write(f'\nStock Trino {version} fallback: `SELECT version()` and Pinot catalog loading passed. '
                                 f'[Server]({evidence["server_url"]}), [official checksum]({evidence["checksum_url"]}): `{evidence["sha1"]}`.\n')
        except BaseException:
            print(log.read_text(errors='replace')[-50000:], file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--plugin', required=True)
    parser.add_argument('--work', required=True)
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    smoke(parser.parse_args())

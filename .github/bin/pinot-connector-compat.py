"""Narrow source-API adaptations for the four imported PR 30881 commits.

These rules preserve each target release's connector/SPI and test framework. New
patches or unfamiliar conflicting source shapes fail for manual backport review.
"""
from difflib import SequenceMatcher
from pathlib import Path
import hashlib
import re
import subprocess

ORIGINAL_RULES = {
    '319be67296195ee06c6ed4971a584d71e853bf62': '22a208c9',
    'bffe951d5ecf6487b4336fe98efe27d24ee893af': '9339800e',
    'c8da4207f25f401e40fc0512f67c047d0bad9e34': '40e624c2',
    'fdd4d88edd6e1aaac1448acea3a3278719799877': '8787edeb',
}
# Context-independent signatures of all added/deleted lines and changed paths.
# A rebase can change context and commit SHA; an amended patch needs new review.
OPERATION_SIGNATURES = {
    '22a208c9': 'e0af07b4b9c546c4e52d704f20d0cb657588d47cdfd4c300b4c09fc91f7bf88a',
    '9339800e': '0dbcff7a924028fe6888a07dd7d34a147f0c02faab9d6bde504f41a3062e40bc',
    '40e624c2': 'd544923ec3bc53054e45f6ec691384e5df074320013448763ec8710f06fd53d9',
    '8787edeb': '3ff8571d0baa50184e6041aaefd6857ea4c24e70c8900c02c18fac84342750d9',
}
PATHS = ['plugin/trino-pinot', 'docs/src/main/sphinx/connector/pinot.md']


def run(*args, check=True, input=None):
    return subprocess.run(args, check=check, input=input, text=True, capture_output=True)


def git(*args):
    return run('git', *args).stdout


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f'Expected one backport anchor, found {text.count(old)}: {old!r}')
    return text.replace(old, new, 1)


def import_group(line):
    if line.startswith('import static '):
        return 'static'
    return 'java' if line.startswith('import java.') else 'external'


def transplant(before, after, target):
    """Replay exact spans with unique original-source anchors, or fail closed."""
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    edits = []
    for kind, i, end, j, stop in SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if kind == 'equal':
            continue
        removed, added = ''.join(old[i:end]), ''.join(new[j:stop])
        if removed:
            if target.count(removed) != 1:
                raise ValueError(f'Expected one changed source span, found {target.count(removed)}: {removed!r}')
            at = target.index(removed)
            edits.append((at, at + len(removed), added))
            continue
        if added.strip() and all(line.startswith('import ') for line in added.splitlines()):
            imports = [existing for existing in target.splitlines(keepends=True)
                       if existing.startswith('import ') and import_group(existing) == import_group(added)]
            following = [existing for existing in imports if existing > added.splitlines(keepends=True)[0]]
            if following:
                at = target.index(following[0])
            elif imports:
                at = target.index(imports[-1]) + len(imports[-1])
            else:
                raise ValueError(f'No matching import group: {added}')
            edits.append((at, at, added))
            continue
        candidates = set()
        for width in range(12, 0, -1):
            left, right = ''.join(old[max(0, i-width):i]), ''.join(old[i:i+width])
            if left and right and target.count(left + right) == 1:
                candidates = {target.index(left + right) + len(left)}
                break
            for anchor, delta in [(left, len(left)), (right, 0)]:
                if anchor and target.count(anchor) == 1:
                    candidates.add(target.index(anchor) + delta)
            if len(candidates) == 1:
                break
            candidates.clear()
        if len(candidates) != 1:
            raise ValueError(f'No unique backport insertion anchor: {added[:100]!r}')
        at = candidates.pop()
        edits.append((at, at, added))
    for at, end, added in sorted(edits, key=lambda edit: edit[0], reverse=True):
        target = target[:at] + added + target[end:]
    return target


def adapt_file(commit, path, before, after, target):
    name = Path(path).name
    if name == 'TestPinotClient.java' and commit[:8] in ['22a208c9', '9339800e', '40e624c2']:
        marker = '    @Test\n    public void testInstanceInfoParsed()'
        prefix = target[:target.index(marker)] if marker in target else target.rsplit('\n}', 1)[0] + '\n\n'
        if commit.startswith('22a208c9'):
            prefix = replace_once(prefix, '        PinotClient pinotClient = new PinotClient(', '        AtomicReference<PinotClient> clientReference = new AtomicReference<>();\n        PinotClient pinotClient = new PinotClient(')
            prefix = replace_once(prefix, 'new IdentityPinotHostMapper()', 'new IdentityPinotHostMapper(clientReference::get)')
            prefix = replace_once(prefix, '                MetadataUtil.BROKER_RESPONSE_NATIVE_JSON_CODEC,', '                MetadataUtil.BROKER_RESPONSE_NATIVE_JSON_CODEC,\n                MetadataUtil.INSTANCE_INFO_JSON_CODEC,')
            prefix = replace_once(prefix, '        assertThat(pinotClient.getAllBrokersForTable("dummy"))', '        clientReference.set(pinotClient);\n        assertThat(pinotClient.getAllBrokersForTable("dummy"))')
        if commit.startswith('9339800e'):
            prefix = replace_once(prefix, '                .setMetadataCacheExpiry(new Duration(1, TimeUnit.MILLISECONDS))\n', '                .setMetadataCacheExpiry(new Duration(1, TimeUnit.MILLISECONDS))\n                .setInstanceConfigRefreshInterval(new Duration(1, TimeUnit.MILLISECONDS))\n')
        if commit.startswith('40e624c2'):
            prefix = replace_once(prefix, '                .setInstanceConfigRefreshInterval(new Duration(1, TimeUnit.MILLISECONDS))\n', '')
        added_imports = [line for line in after.splitlines(keepends=True) if line.startswith('import ') and line not in before.splitlines(keepends=True)]
        for line in added_imports:
            imports = [existing for existing in prefix.splitlines(keepends=True) if existing.startswith('import ') and import_group(existing) == import_group(line)]
            following = [existing for existing in imports if existing > line]
            if following:
                prefix = replace_once(prefix, following[0], line + following[0])
            else:
                prefix = replace_once(prefix, imports[-1], imports[-1] + line)
        return prefix + after[after.index(marker):], 'preserve-historical-broker-test-and-update-added-tests'
    if name == 'PinotPageSourceProvider.java' and commit.startswith('40e624c2'):
        statement = '                clusterInfoFetcher.cacheInstanceInfoFromSplit(pinotSplit.getSegmentHost().orElseThrow(), pinotSplit.getInstanceInfo().orElseThrow());\n'
        if '            case SEGMENT:\n' in target:
            return replace_once(target, '            case SEGMENT:\n', '            case SEGMENT:\n' + statement), 'historical-segment-switch'
        return replace_once(target, '            case SEGMENT -> {\n', '            case SEGMENT -> {\n' + statement), 'segment-switch-context'
    if name == 'PinotGrpcDataFetcher.java' and commit.startswith('8787edeb'):
        target = replace_once(target, '            closer.close();', '            synchronized (closer) {\n                closer.close();\n            }')
        registrations = re.findall(r'^                closer.register\(queryClient(?:::close)?\);$', target, re.M)
        if len(registrations) != 1:
            raise ValueError('Unknown Pinot gRPC registration shape')
        original = registrations[0]
        return replace_once(target, original, '                // computeIfAbsent runs concurrently for different servers, and Closer is not thread safe\n                synchronized (closer) {\n    ' + original + '\n                }'), 'historical-grpc-client-close'
    if name == 'TestPinotSplitManager.java' and commit.startswith('40e624c2'):
        assertions = '        assertThat(split.getInstanceInfo()).isPresent();\n        assertThat(split.getInstanceInfo().orElseThrow().instanceName()).isEqualTo(split.getSegmentHost().orElseThrow());\n        assertThat(split.getInstanceInfo().orElseThrow().hostName()).isNotEqualTo(split.getSegmentHost().orElseThrow());\n'
        anchors = ['        assertTrue(split.getSegmentHost().isPresent());\n', '        assertThat(split.getSegmentHost()).isPresent();\n', '        assertThat(split.getSegmentHost().isPresent()).isTrue();\n']
        found = [anchor for anchor in anchors if anchor in target]
        if len(found) != 1:
            raise ValueError('Unknown split-manager assertions')
        target = replace_once(target, found[0], found[0] + assertions)
        if 'import static org.assertj.core.api.Assertions.assertThat;' not in target:
            target = replace_once(target, 'import static org.testng.Assert.assertEquals;', 'import static org.assertj.core.api.Assertions.assertThat;\nimport static org.testng.Assert.assertEquals;')
        return target, 'historical-split-assertions'
    return transplant(before, after, target), 'exact-span-context'


def resolve_conflicts(commit, version):
    """Resolve known source shapes after a failed git apply --3way --index."""
    trailers = re.findall(r'\(cherry picked from commit ([0-9a-f]{40})\)', git('show', '-s', '--format=%B', commit))
    rules = {ORIGINAL_RULES[original] for original in trailers if original in ORIGINAL_RULES}
    if len(rules) != 1 or not 429 <= int(version) <= 483:
        raise ValueError(f'No reviewed compatibility rules for {commit} on Trino {version}')
    rule_commit = rules.pop()
    operation_diff = git('diff', '--no-ext-diff', '--unified=0', commit + '^', commit, '--', *PATHS)
    operations = [line for line in operation_diff.splitlines() if line.startswith('diff --git ')
                  or (line.startswith(('+', '-')) and not line.startswith(('+++ ', '--- ')))]
    signature = hashlib.sha256('\n'.join(operations).encode()).hexdigest()
    if signature != OPERATION_SIGNATURES[rule_commit]:
        raise ValueError(f'Imported patch {commit} changed; compatibility rules need review')
    conflicts = git('diff', '--name-only', '--diff-filter=U').splitlines()
    reports = []
    if not conflicts:
        # git apply is transactional when the historical TestInstance deletion
        # differs: replay each same-commit file to expose its merge stages.
        if git('diff', '--cached', '--name-only').strip() or git('diff', '--name-only').strip():
            raise ValueError('Failed apply left changes without conflict stages')
        paths = git('diff-tree', '--no-commit-id', '--name-only', '-r', commit, '--', *PATHS).splitlines()
        for path in paths:
            patch = git('diff', commit + '^', commit, '--', path)
            result = run('git', 'apply', '--3way', '--index', input=patch, check=False)
            if result.returncode:
                if path.endswith('/TestInstance.java') and rule_commit == '22a208c9':
                    target = git('show', f'HEAD:{path}')
                    if 'public class TestInstance' not in target or 'new ServerInstance(' not in target:
                        raise ValueError('Unknown historical TestInstance deletion')
                    run('git', 'rm', '-f', path)
                    reports.append({'file': path, 'rule': 'replace-historical-instance-test'})
                elif path in git('diff', '--name-only', '--diff-filter=U').splitlines():
                    conflicts.append(path)
                else:
                    raise ValueError(f'Unrecognized backport apply failure for {path}: {result.stderr}')
    for path in conflicts:
        if not any(path == prefix or path.startswith(prefix + '/') for prefix in PATHS):
            raise ValueError(f'Unsupported conflicting path: {path}')
        if path.endswith('/TestInstance.java') and rule_commit == '22a208c9':
            run('git', 'rm', '-f', path)
            reports.append({'file': path, 'rule': 'replace-historical-instance-test'})
            continue
        before, after = git('show', f'{commit}^:{path}'), git('show', f'{commit}:{path}')
        target = git('show', f':2:{path}')
        adapted, rule = adapt_file(rule_commit, path, before, after, target)
        if re.search(r'^(<<<<<<<|=======|>>>>>>>)', adapted, re.M):
            raise ValueError(f'Unresolved conflict in {path}')
        Path(path).write_text(adapted)
        run('git', 'add', '--sparse', path)
        reports.append({'file': path, 'rule': rule})
    if git('diff', '--name-only', '--diff-filter=U').strip():
        raise ValueError('Compatibility replay left unresolved conflicts')
    return reports


def normalize(version):
    """Use only APIs/language features available to the target release."""
    version = int(version)
    if version < 429:
        raise ValueError('Trino releases before 429 are unsupported')
    reports = []
    root = Path('plugin/trino-pinot/src')
    targets = [root / 'main/java/io/trino/plugin/pinot/client/PinotClient.java',
               root / 'test/java/io/trino/plugin/pinot/TestPinotClient.java',
               root / 'test/java/io/trino/plugin/pinot/TestInstanceInfo.java',
               root / 'test/java/io/trino/plugin/pinot/TestPinotSplit.java']
    config = (root / 'main/java/io/trino/plugin/pinot/PinotConfig.java').read_text()
    for path in targets:
        text = path.read_text()
        original = text
        rules = []
        if path.name == 'PinotClient.java' and version <= 480:
            needle = 'config.getInstanceConfigRefreshInterval().toJavaTime()'
            if needle in text:
                text = replace_once(text, needle, 'config.getInstanceConfigRefreshInterval().roundTo(TimeUnit.MILLISECONDS), TimeUnit.MILLISECONDS')
                rules.append('historical-airlift-duration')
        if path.name.startswith('Test'):
            if version <= 433 and 'import org.junit.jupiter.api.Test;' in text:
                text = replace_once(text, 'import org.junit.jupiter.api.Test;', 'import org.testng.annotations.Test;')
                rules.append('historical-testng')
            if version < 447:
                text, count = re.subn(r'\b_ ->', 'request ->', text)
                if count:
                    rules.append('named-lambda-parameters')
            if path.name == 'TestPinotClient.java' and 'setControllerUrls(String ' in config:
                text, count = re.subn(r'\.setControllerUrls\(ImmutableList\.of\("localhost:7900"\)\)', '.setControllerUrls("localhost:7900")', text)
                if count:
                    rules.append('historical-controller-url-setter')
                if 'ImmutableList' not in text.replace('import com.google.common.collect.ImmutableList;', ''):
                    text = text.replace('import com.google.common.collect.ImmutableList;\n', '')
        if text != original:
            path.write_text(text)
            run('git', 'add', str(path))
            reports.append({'file': str(path), 'rules': rules})
    # Assert.assertEventually moved into this module in 434; Pinot started
    # declaring its direct test dependency in 452.
    if 434 <= version <= 451:
        pom = Path('plugin/trino-pinot/pom.xml')
        text = pom.read_text()
        if '<artifactId>trino-testing-services</artifactId>' not in text:
            anchor = ("        <dependency>\n"
                      "            <groupId>io.trino</groupId>\n"
                      "            <artifactId>trino-tpch</artifactId>\n"
                      "            <scope>test</scope>\n"
                      "        </dependency>\n")
            dependency = anchor.replace('trino-tpch', 'trino-testing-services')
            pom.write_text(replace_once(text, anchor, dependency + '\n' + anchor))
            run('git', 'add', '--sparse', str(pom))
            reports.append({'file': str(pom), 'rules': ['historical-testing-services-test-dependency']})
    return reports

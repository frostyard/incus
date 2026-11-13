"""Tests for sync-docker-build.py and the release checks it emits.

Standard library only: no PyYAML, no other dependency. The generator is run as
a script against a temporary copy of the layout (`.github/workflows/builds.yml`
plus the generator itself), so no test writes to the repository. The release
package checks are extracted from the generated workflow with plain text
parsing and run with `bash` against fake `.deb` files and a stub `dpkg-deb`.

Run them with:

    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATOR_NAME = 'sync-docker-build.py'
GENERATOR = os.path.join(REPO_ROOT, GENERATOR_NAME)
BUILDS_YML = os.path.join(REPO_ROOT, '.github', 'workflows', 'builds.yml')
BUILDS_DOCKER_YML = os.path.join(REPO_ROOT, '.github', 'workflows',
                                 'builds-docker.yml')

# The 5 packages the release jobs of frostyard/incus must hold, once each.
RELEASE_PACKAGES = ['incus', 'incus-base', 'incus-client', 'incus-extra',
                    'incus-ui-canonical']

FORKY_VERSION = '1:7.6-debian14-202611131200~deb14'
FORKY_TAG = '7.6-debian14-202611131200'
TRIXIE_VERSION = '1:7.6-debian13-202611131200'
TRIXIE_TAG = '7.6-debian13-202611131200'

FORKY_CODENAME_LINE = '[ "${PKGOS}" = "debian-14" ] && CODENAME=forky'
TRIXIE_CODENAME_LINE = '[ "${PKGOS}" = "debian-13" ] && CODENAME=trixie'
RESOLUTE_CODENAME_LINE = '[ "${PKGOS}" = "ubuntu-26.04" ] && CODENAME=resolute'


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def indent_of(line):
    return len(line) - len(line.lstrip())


def split_jobs(text):
    """Split a workflow into {job id: job text} by the 2-space job keys.

    The workflow files here are plain text with no anchors or aliases, so this
    small parser is enough; it never tries to be a YAML parser. `jobs:` must be
    a top-level key. Lines before the first job key, comments included, are
    ignored; comments between 2 jobs stay with the earlier job.
    """
    lines = text.splitlines()
    try:
        start = lines.index('jobs:')
    except ValueError:  # pragma: no cover - a broken workflow file
        raise AssertionError('the workflow has no top-level `jobs:` line')

    jobs = {}
    name = None
    body = []
    for line in lines[start + 1:]:
        m = re.match(r'^  ([\w-]+):\s*$', line)
        if m:
            if name is not None:
                jobs[name] = '\n'.join(body)
            name = m.group(1)
            body = [line]
            continue
        if name is not None:
            body.append(line)
    if name is not None:
        jobs[name] = '\n'.join(body)
    return jobs


def extract_run_script(job_text, step_name):
    """Return the dedented `run: |` script of a named step in `job_text`."""
    lines = job_text.splitlines()
    step = '- name: %s' % step_name
    try:
        step_line = next(i for i, l in enumerate(lines) if l.strip() == step)
    except StopIteration:  # pragma: no cover - a renamed step
        raise AssertionError('the job has no step named %r' % step_name)

    run_line = None
    for i in range(step_line + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith('- name: '):
            break
        if stripped in ('run: |', 'run: |2', 'run: |-'):
            run_line = i
            break
    if run_line is None:  # pragma: no cover - a renamed key
        raise AssertionError('step %r has no `run: |` block' % step_name)

    body_indent = indent_of(lines[run_line]) + 2
    body = []
    for line in lines[run_line + 1:]:
        if line.strip() == '':
            body.append('')
            continue
        if indent_of(line) < body_indent:
            break
        body.append(line[body_indent:])
    return '\n'.join(body) + '\n'


def drop_lines(text, needle):
    """Remove every line that contains `needle`; fail unless some line did."""
    out = [line for line in text.splitlines() if needle not in line]
    if len(out) == len(text.splitlines()):  # pragma: no cover - bases moved
        raise AssertionError('no line contains %r' % needle)
    return '\n'.join(out) + '\n'


def rename_if_operands(text):
    """Rename every `ubuntu-26.04` operand of an `if ` or `if:` line.

    Only those 2 kinds of line are touched, because those are the operands
    `sync-docker-build.py` rewrites for forky; the matrix entry and the
    CODENAME table must keep their `ubuntu-26.04` spelling.
    """
    out = []
    changed = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith('if '):
            new = line.replace('"ubuntu-26.04"', '"ubuntu-24.04"')
        elif stripped.startswith('if:'):
            new = line.replace("'ubuntu-26.04'", "'ubuntu-24.04'")
        else:
            out.append(line)
            continue
        changed += new != line
        out.append(new)
    if not changed:  # pragma: no cover - the upstream file moved on
        raise AssertionError('builds.yml has no `if`/`if:` line with '
                             '`ubuntu-26.04`, so this test proves nothing')
    return '\n'.join(out) + '\n'


class GeneratorTestCase(unittest.TestCase):
    """Helpers that run the generator on a temporary layout."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='incus-generator-test-')
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def run_generator(self, builds_yml_text):
        """Run the generator on `builds_yml_text`; return (process, output).

        `output` is the generated builds-docker.yml text, or None when the
        generator failed before writing it.
        """
        root = tempfile.mkdtemp(dir=self.tmpdir)
        workflows = os.path.join(root, '.github', 'workflows')
        os.makedirs(workflows)
        with open(os.path.join(workflows, 'builds.yml'), 'w',
                  encoding='utf-8') as f:
            f.write(builds_yml_text)
        generator = os.path.join(root, GENERATOR_NAME)
        shutil.copyfile(GENERATOR, generator)

        process = subprocess.run([sys.executable, generator], cwd=root,
                                 capture_output=True, text=True)
        out = os.path.join(workflows, 'builds-docker.yml')
        generated = read(out) if os.path.exists(out) else None
        return process, generated

    def generate_ok(self, builds_yml_text=None):
        if builds_yml_text is None:
            builds_yml_text = read(BUILDS_YML)
        process, generated = self.run_generator(builds_yml_text)
        self.assertEqual(process.returncode, 0,
                         'the generator failed:\n%s' % process.stderr)
        self.assertIsNotNone(generated)
        return generated


class GeneratedFileTests(GeneratorTestCase):
    """The committed builds-docker.yml and its 4 jobs."""

    @classmethod
    def setUpClass(cls):
        cls.builds_yml = read(BUILDS_YML)
        cls.committed = read(BUILDS_DOCKER_YML)
        cls.jobs = split_jobs(cls.committed)

    def test_committed_file_matches_generator(self):
        """builds-docker.yml is exactly what the generator emits (drift)."""
        self.assertMultiLineEqual(self.committed,
                                  self.generate_ok(self.builds_yml))

    def test_jobs(self):
        self.assertEqual(
            sorted(self.jobs),
            ['build-incus', 'build-incus-forky', 'release', 'release-forky',
             'request-publication', 'request-publication-forky'])

    def assert_has_fragment(self, text, fragment, where):
        """assertIn, with a short message instead of the whole job text."""
        self.assertTrue(fragment in text,
                        '%r is missing from %s' % (fragment, where))

    def assert_no_fragment(self, text, fragment, where):
        self.assertTrue(fragment not in text,
                        '%r must not be in %s' % (fragment, where))

    def test_trixie_build_job(self):
        job = self.jobs['build-incus']
        for fragment in ['name: Build Incus (Debian Trixie)',
                         'image: debian:trixie',
                         'OS_NAME: "debian-13"',
                         'PKGOS: "debian-13"']:
            with self.subTest(fragment=fragment):
                self.assert_has_fragment(job, fragment, 'the trixie build job')
        for fragment in ['debian-14', '~deb14', 'rpcsvc-proto',
                         'CODENAME=forky', 'debian:forky']:
            with self.subTest(absent=fragment):
                self.assert_no_fragment(job, fragment, 'the trixie build job')

    def test_trixie_request_publication(self):
        self.assertIn('"codenames": ["trixie"]',
                      self.jobs['request-publication'])

    def test_forky_build_job(self):
        job = self.jobs['build-incus-forky']
        for fragment in ['name: Build Incus (Debian Forky)',
                         'image: debian:forky',
                         'OS_NAME: "debian-14"',
                         'PKGOS: "debian-14"',
                         'rpcsvc-proto',
                         '$(date -u +%Y%m%d%H%M)~deb14 --force-distribution',
                         'name: debian-forky-amd64',
                         FORKY_CODENAME_LINE,
                         TRIXIE_CODENAME_LINE,
                         RESOLUTE_CODENAME_LINE]:
            with self.subTest(fragment=fragment):
                self.assert_has_fragment(job, fragment, 'the forky build job')

        lines = job.splitlines()
        for i, line in enumerate(lines):
            if line.strip() == TRIXIE_CODENAME_LINE:
                self.assertEqual(lines[i + 1].strip(), FORKY_CODENAME_LINE,
                                 'the forky CODENAME branch must come right '
                                 'after the trixie one')
                break
        else:  # pragma: no cover - covered by the assertion above
            self.fail('the forky build job has no CODENAME=trixie line')

    def test_forky_build_job_has_no_ubuntu_26_04_operand(self):
        lines = [l for l in self.jobs['build-incus-forky'].splitlines()
                 if l.lstrip().startswith(('if ', 'if:'))]
        self.assertTrue(lines, 'the forky build job has no conditional lines')
        for line in lines:
            with self.subTest(line=line.strip()):
                self.assertNotIn('ubuntu-26.04', line)

    def test_rpcsvc_proto_follows_python3_yaml(self):
        lines = self.jobs['build-incus-forky'].splitlines()
        i = next(i for i, l in enumerate(lines)
                 if l.strip() == 'python3-yaml \\')
        self.assertEqual(lines[i + 1].strip(), 'rpcsvc-proto \\')


class GeneratorFailTests(GeneratorTestCase):
    """Each required builds.yml pattern gets a loud failure when it is gone."""

    def assert_generator_fails(self, builds_yml_text, message):
        process, generated = self.run_generator(builds_yml_text)
        self.assertNotEqual(process.returncode, 0,
                            'the generator accepted a builds.yml with the '
                            'pattern removed')
        self.assertIn(message, process.stderr)
        self.assertIsNone(generated,
                          'the generator wrote a file before failing')

    def test_fails_without_trixie_codename(self):
        text = drop_lines(read(BUILDS_YML), TRIXIE_CODENAME_LINE)
        self.assert_generator_fails(
            text, 'the Debian 14 (forky) CODENAME cannot be added')

    def test_fails_without_dch_line(self):
        text = drop_lines(read(BUILDS_YML), 'dch --package incus --create')
        self.assert_generator_fails(
            text, 'version cannot get its ~deb14 marker')

    def test_fails_without_pkgos(self):
        text = drop_lines(read(BUILDS_YML), 'PKGOS: ${{ matrix.os }}')
        self.assert_generator_fails(
            text, 'cannot pin PKGOS to debian-14')

    def test_fails_without_python3_venv(self):
        text = drop_lines(read(BUILDS_YML), '            python3-venv \\')
        self.assert_generator_fails(
            text, 'cannot add rpcsvc-proto (rpcgen)')

    def test_fails_without_artifact_name(self):
        text = drop_lines(read(BUILDS_YML),
                          'name: ${{ matrix.os }}-${{ matrix.arch }}')
        self.assert_generator_fails(
            text, 'artifact cannot be named debian-forky-amd64')

    def test_fails_without_ubuntu_26_04_if_operands(self):
        text = rename_if_operands(read(BUILDS_YML))
        self.assert_generator_fails(text, 'cannot be derived')


class ReleaseCheckTests(GeneratorTestCase):
    """The `Check the packages` scripts, run with fake .deb files.

    `dpkg-deb` is stubbed: it answers `-f FILE Field` from a sidecar file
    `FILE.fields` that holds `Field=value` lines, so a test case states the
    package name, version and architecture of each fake `.deb` directly.
    """

    STUB = '''#!/bin/sh
set -eu
if [ "$1" != "-f" ]; then
  echo "stub dpkg-deb: unsupported arguments: $*" >&2
  exit 2
fi
fields="$2.fields"
if [ ! -f "$fields" ]; then
  echo "stub dpkg-deb: no field file for $2" >&2
  exit 1
fi
sed -n "s/^$3=//p" "$fields" | head -n 1
'''

    @classmethod
    def setUpClass(cls):
        cls.jobs = split_jobs(read(BUILDS_DOCKER_YML))
        cls.scripts = {
            'release': extract_run_script(cls.jobs['release'],
                                          'Check the packages'),
            'release-forky': extract_run_script(cls.jobs['release-forky'],
                                                'Check the packages'),
        }

    def run_check(self, job, debs):
        """Run a release check on `debs`, a list of (file stem, package,
        version, architecture). Return (process, GITHUB_OUTPUT text)."""
        root = tempfile.mkdtemp(dir=self.tmpdir)
        dist = os.path.join(root, 'dist')
        os.makedirs(dist)
        for stem, package, version, arch in debs:
            path = os.path.join(dist, stem + '.deb')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('not a real package\n')
            with open(path + '.fields', 'w', encoding='utf-8') as f:
                f.write('Package=%s\nVersion=%s\nArchitecture=%s\n'
                        % (package, version, arch))

        stub_dir = os.path.join(root, 'bin')
        os.makedirs(stub_dir)
        stub = os.path.join(stub_dir, 'dpkg-deb')
        with open(stub, 'w', encoding='utf-8') as f:
            f.write(self.STUB)
        os.chmod(stub, 0o755)

        script = os.path.join(root, 'check.sh')
        with open(script, 'w', encoding='utf-8') as f:
            f.write(self.scripts[job])

        output_path = os.path.join(root, 'github_output')
        env = dict(os.environ)
        env['PATH'] = stub_dir + os.pathsep + env.get('PATH', '/usr/bin:/bin')
        env['GITHUB_OUTPUT'] = output_path

        process = subprocess.run(['bash', script], cwd=root, env=env,
                                 capture_output=True, text=True)
        output = read(output_path) if os.path.exists(output_path) else ''
        return process, output

    @staticmethod
    def debs(version, package_names=None, arch='amd64', stems=None):
        """A full release set of fake .deb entries for `version`."""
        names = list(package_names if package_names is not None
                     else RELEASE_PACKAGES)
        names += [''] * (5 - len(names))
        if stems is None:
            stems = [n or 'filler' for n in names]
        return [(stem, name, version, arch)
                for stem, name in zip(stems, names)]

    # -- forky ------------------------------------------------------------

    def test_forky_accepts_a_valid_release(self):
        process, output = self.run_check('release-forky',
                                         self.debs(FORKY_VERSION))
        self.assertEqual(process.returncode, 0,
                         'the check rejected a valid release:\n%s\n%s'
                         % (process.stdout, process.stderr))
        self.assertEqual(output, 'tag=%s\n' % FORKY_TAG)

    def test_forky_rejects(self):
        cases = {
            'empty dist': (
                [],
                'expected the packages: %s; found: none'
                % ' '.join(RELEASE_PACKAGES)),
            'missing package': (
                self.debs(FORKY_VERSION,
                          RELEASE_PACKAGES[:4],
                          stems=RELEASE_PACKAGES[:4]),
                'expected the packages: %s; found: %s'
                % (' '.join(RELEASE_PACKAGES),
                   ' '.join(RELEASE_PACKAGES[:4]))),
            'extra package': (
                self.debs(FORKY_VERSION)
                + [('incus-extra-2', 'incus-extra-2', FORKY_VERSION, 'amd64')],
                'expected the packages: %s; found: %s'
                % (' '.join(RELEASE_PACKAGES),
                   ' '.join(sorted(RELEASE_PACKAGES + ['incus-extra-2'])))),
            'duplicate package name': (
                self.debs(FORKY_VERSION,
                          RELEASE_PACKAGES[:4] + ['incus-client'],
                          stems=['incus', 'incus-base', 'incus-client',
                                 'incus-extra', 'incus-client-2']),
                'expected the packages: %s' % ' '.join(RELEASE_PACKAGES)),
            'mixed versions': (
                self.debs(FORKY_VERSION)[:4]
                + [('incus-ui-canonical', 'incus-ui-canonical',
                    '1:7.6-debian14-202611131201~deb14', 'amd64')],
                'but another package has %s' % FORKY_VERSION),
            'wrong architecture': (
                self.debs(FORKY_VERSION, arch='arm64'),
                'is built for arm64, not amd64'),
            'Debian 13 version': (
                self.debs('1:7.6-debian13-202611131200'),
                'which is not a Debian 14 build'),
            'version without ~deb14': (
                self.debs('1:7.6-debian14-202611131200'),
                'which is not a Debian 14 build'),
            'version with ~deb13': (
                self.debs('1:7.6-debian14-202611131200~deb13'),
                'which is not a Debian 14 build'),
        }

        for label, (debs, message) in cases.items():
            with self.subTest(case=label):
                process, output = self.run_check('release-forky', debs)
                self.assertNotEqual(process.returncode, 0,
                                    'the check accepted: %s' % label)
                self.assertIn('::error::', process.stdout)
                self.assertIn(message, process.stdout)
                self.assertEqual(output, '',
                                 'the check wrote a tag for: %s' % label)

    # -- trixie -----------------------------------------------------------

    def test_trixie_accepts_a_valid_release(self):
        process, output = self.run_check('release',
                                         self.debs(TRIXIE_VERSION))
        self.assertEqual(process.returncode, 0,
                         'the check rejected a valid release:\n%s\n%s'
                         % (process.stdout, process.stderr))
        self.assertEqual(output, 'tag=%s\n' % TRIXIE_TAG)

    def test_trixie_rejects_a_wrong_version_marker(self):
        process, output = self.run_check(
            'release', self.debs(TRIXIE_VERSION + '~deb14'))
        self.assertNotEqual(process.returncode, 0,
                            'the trixie check accepted a ~deb14 version')
        self.assertIn('which is not a Debian 13 build', process.stdout)
        self.assertEqual(output, '')


if __name__ == '__main__':
    unittest.main()

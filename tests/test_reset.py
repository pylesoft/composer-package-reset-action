import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ACTION = Path(__file__).resolve().parents[1] / 'reset.php'
CORE = ['floorbox/cbox-framework', 'floorbox/cbox-admin-module',
        'floorbox/cbox-storefront-module', 'pylesoft/admin',
        'floorbox/pyle-erp-bridge-module']


class ResetIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'app'
        self.repo.mkdir()
        self.remote = self.root / 'remote.git'
        self.command(['git', 'init', '--bare', str(self.remote)])
        self.command(['git', 'init', '-b', 'master'])
        self.command(['git', 'config', 'user.name', 'Fixture'])
        self.command(['git', 'config', 'user.email', 'fixture@example.test'])
        self.command(['git', 'remote', 'add', 'origin', str(self.remote)])
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        gh = self.bin / 'gh'
        gh.write_text('''#!/usr/bin/env python3
import json, sys
repo = sys.argv[2]
if repo.startswith('repos/GVPtools/'):
    print('master')
elif repo in ['repos/pylesoft/admin', 'repos/floorbox/pyle-erp-bridge-module']:
    print('master')
elif repo.startswith('repos/floorbox/'):
    print('main')
else:
    sys.exit('Unexpected GitHub repository lookup: ' + repo)
''')
        gh.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        RESET_DEVELOPMENT_BRANCH='master', RESET_PUSH='true',
                        GITHUB_OUTPUT=str(self.root / 'output'),
                        GITHUB_STEP_SUMMARY=str(self.root / 'summary'))

    def command(self, command, check=True, env=None):
        result = subprocess.run(command, cwd=self.repo, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if check and result.returncode:
            self.fail(f'{command}:\n{result.stdout}')
        return result

    def manifest(self, directory='.', targets=None, development=False):
        directory = self.repo / directory
        directory.mkdir(parents=True, exist_ok=True)
        targets = CORE if targets is None else targets
        packages = [{'name': name, 'version': version, 'type': 'metapackage'}
                    for name in targets for version in ['1.0.0', 'dev-feature', 'dev-main', 'dev-master']]
        packages.append({'name': 'fixture/unrelated', 'version': '1.0.0', 'type': 'metapackage'})
        manifest = {'name': 'fixture/app', 'description': 'Reset test fixture',
                    'license': 'proprietary', 'type': 'project',
                    'require': {'fixture/unrelated': '^1.0'},
                    'repositories': [{'type': 'package', 'package': packages}, {'packagist.org': False}],
                    'minimum-stability': 'dev'}
        section = 'require-dev' if development else 'require'
        manifest.setdefault(section, {}).update({name: '1.0.0' for name in targets})
        path = directory / 'composer.json'
        path.write_text(json.dumps(manifest, indent=4) + '\n')
        self.command(['composer', 'update', '--working-dir=' + str(directory),
                      '--no-install', '--no-scripts', '--no-interaction', '--no-progress'])
        return path

    def publish_fixture(self):
        self.command(['git', 'add', '.'])
        self.command(['git', 'commit', '-m', 'Fixture'])
        self.command(['git', 'push', '-u', 'origin', 'master'])
        return self.command(['git', 'rev-parse', 'HEAD']).stdout.strip()

    def reset(self, **options):
        env = dict(self.env, **options)
        return self.command(['php', str(ACTION)], check=False, env=env)

    def locked(self, directory='.'):
        lock = json.loads((self.repo / directory / 'composer.lock').read_text())
        return {p['name']: p['version'] for p in lock['packages'] + lock['packages-dev']}

    def test_resets_all_core_constraints_pushes_lock_and_repeats_without_commit(self):
        path = self.manifest()
        manifest = json.loads(path.read_text())
        manifest['require'][CORE[0]] = 'dev-feature as dev-main'
        manifest['require'][CORE[1]] = 'dev-feature'
        manifest['repositories'][0]['package'].append(
            {'name': 'fixture/unrelated', 'version': '1.1.0', 'type': 'metapackage'})
        path.write_text(json.dumps(manifest, indent=4) + '\n')
        before = self.publish_fixture()
        result = self.reset()
        self.assertEqual(result.returncode, 0, result.stdout)
        constraints = json.loads(path.read_text())['require']
        expected = {CORE[0]: 'dev-main', CORE[1]: 'dev-main', CORE[2]: 'dev-main',
                    CORE[3]: 'dev-master', CORE[4]: 'dev-master'}
        self.assertEqual({name: constraints[name] for name in CORE}, expected)
        self.assertEqual({name: self.locked()[name] for name in CORE}, expected)
        self.assertEqual(self.locked()['fixture/unrelated'], '1.0.0')
        after = self.command(['git', 'rev-parse', 'HEAD']).stdout.strip()
        self.assertNotEqual(before, after)
        self.assertEqual(after, self.command(['git', 'rev-parse', 'origin/master']).stdout.strip())
        self.assertEqual(self.command(['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD']).stdout.splitlines(),
                         ['composer.json', 'composer.lock'])
        self.assertEqual(self.reset().returncode, 0)
        self.assertEqual(after, self.command(['git', 'rev-parse', 'HEAD']).stdout.strip())
        self.assertIn('outcome=unchanged', (self.root / 'output').read_text())

    def test_absent_packages_stay_absent_and_require_dev_stays_in_its_section(self):
        path = self.manifest(targets=[CORE[3]], development=True)
        self.publish_fixture()
        result = self.reset()
        self.assertEqual(result.returncode, 0, result.stdout)
        manifest = json.loads(path.read_text())
        self.assertEqual(manifest['require-dev'], {'pylesoft/admin': 'dev-master'})
        self.assertEqual(manifest['require'], {'fixture/unrelated': '^1.0'})

    def test_skips_no_target_manifest_without_a_commit(self):
        self.manifest(targets=[])
        before = self.publish_fixture()
        result = self.reset()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(before, self.command(['git', 'rev-parse', 'HEAD']).stdout.strip())
        self.assertIn('outcome=skipped', (self.root / 'output').read_text())

    def test_dummy_mapping_uses_the_dummy_repository_default(self):
        path = self.manifest(targets=[CORE[0]])
        self.publish_fixture()
        result = self.reset(RESET_PACKAGE_REPOSITORIES=json.dumps({CORE[0]: 'GVPtools/mothership-dummy-pyle'}))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(path.read_text())['require'][CORE[0]], 'dev-master')
        self.assertEqual(self.locked()[CORE[0]], 'dev-master')

    def test_missing_second_lock_does_not_push_a_partial_reset(self):
        self.manifest()
        second = self.manifest('nested', targets=[CORE[3]])
        second.with_suffix('.lock').unlink()
        before = self.publish_fixture()
        result = self.reset(RESET_COMPOSER_FILES='["composer.json", "nested/composer.json"]')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, self.command(['git', 'rev-parse', 'origin/master']).stdout.strip())
        self.assertEqual(before, self.command(['git', 'rev-parse', 'HEAD']).stdout.strip())

    def test_multiple_manifests_are_persisted_in_one_commit(self):
        self.manifest(targets=[CORE[0]])
        self.manifest('nested', targets=[CORE[3]])
        before = self.publish_fixture()
        result = self.reset(RESET_COMPOSER_FILES='["composer.json", "nested/composer.json"]')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.locked()[CORE[0]], 'dev-main')
        self.assertEqual(self.locked('nested')[CORE[3]], 'dev-master')
        self.assertEqual(self.command(['git', 'rev-list', '--count', before + '..HEAD']).stdout.strip(), '1')
        self.assertEqual(self.command(['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD']).stdout.splitlines(),
                         ['composer.json', 'composer.lock', 'nested/composer.json', 'nested/composer.lock'])

    def test_path_packages_use_restored_manifests_and_refresh_shared_core_locks(self):
        framework = self.manifest('packages/framework', targets=[CORE[4]])
        framework_payload = json.loads(framework.read_text())
        framework_payload['name'] = CORE[0]
        framework.write_text(json.dumps(framework_payload, indent=4) + '\n')
        app = self.manifest('apps/laravel', targets=[CORE[4]])
        app_payload = json.loads(app.read_text())
        app_payload['require'].pop(CORE[4])
        app_payload['require'][CORE[0]] = 'dev-main'
        app_payload['repositories'].insert(0, {
            'type': 'path', 'url': '../../packages/framework',
            'options': {'versions': {CORE[0]: 'dev-main'}},
        })
        app.write_text(json.dumps(app_payload, indent=4) + '\n')
        self.command(['composer', 'update', '--working-dir=' + str(app.parent),
                      '--no-install', '--no-scripts', '--no-interaction', '--no-progress'])
        self.publish_fixture()

        result = self.reset(RESET_COMPOSER_FILES='["apps/laravel/composer.json", "packages/framework/composer.json"]')

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.locked('apps/laravel')[CORE[4]], 'dev-master')
        self.assertEqual(self.locked('packages/framework')[CORE[4]], 'dev-master')
        app_lock = json.loads(app.with_suffix('.lock').read_text())
        locked_framework = next(package for package in app_lock['packages'] if package['name'] == CORE[0])
        self.assertEqual(locked_framework['require'][CORE[4]], 'dev-master')
        self.assertNotIn(CORE[4], json.loads(app.read_text())['require'])
        self.assertEqual(self.locked('apps/laravel')['fixture/unrelated'], '1.0.0')
        after = self.command(['git', 'rev-parse', 'HEAD']).stdout.strip()
        repeat = self.reset(RESET_COMPOSER_FILES='["apps/laravel/composer.json", "packages/framework/composer.json"]')
        self.assertEqual(repeat.returncode, 0, repeat.stdout)
        self.assertEqual(after, self.command(['git', 'rev-parse', 'HEAD']).stdout.strip())

    def test_solver_failure_does_not_commit_or_push(self):
        path = self.manifest(targets=[CORE[0]])
        payload = json.loads(path.read_text())
        payload['repositories'][0]['package'] = [package for package in payload['repositories'][0]['package']
                                                  if package['version'] != 'dev-main']
        path.write_text(json.dumps(payload, indent=4) + '\n')
        before = self.publish_fixture()
        result = self.reset()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('composer failed', result.stdout)
        self.assertEqual(before, self.command(['git', 'rev-parse', 'HEAD']).stdout.strip())
        self.assertEqual(before, self.command(['git', 'rev-parse', 'origin/master']).stdout.strip())

    def test_rejects_wrong_branch_dirty_files_and_traversing_paths(self):
        self.manifest()
        before = self.publish_fixture()
        for options in [{'RESET_DEVELOPMENT_BRANCH': 'production'},
                        {'RESET_COMPOSER_FILES': '["../composer.json"]'}]:
            with self.subTest(options=options):
                result = self.reset(**options)
                self.assertNotEqual(result.returncode, 0)
        (self.repo / 'unrelated.txt').write_text('Do not commit me')
        self.assertNotEqual(self.reset().returncode, 0)
        self.assertEqual(before, self.command(['git', 'rev-parse', 'origin/master']).stdout.strip())


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Restore present Pyle requirements and their locks, then push one commit."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

PACKAGE_REPOSITORIES = {
    'floorbox/cbox-framework': 'floorbox/cbox-framework',
    'floorbox/cbox-admin-module': 'floorbox/cbox-admin-module',
    'floorbox/cbox-storefront-module': 'floorbox/cbox-storefront-module',
    'pylesoft/admin': 'pylesoft/admin',
    'floorbox/pyle-erp-bridge-module': 'floorbox/pyle-erp-bridge-module',
}


def command(arguments, directory=None):
    result = subprocess.run(arguments, cwd=directory, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f'{arguments[0]} failed:\n{result.stdout}')
    return result.stdout.strip()


def json_object(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f'{path} must contain a JSON object.')
    return value


def package_repositories():
    overrides = json.loads(os.environ.get('RESET_PACKAGE_REPOSITORIES', '{}'))
    if not isinstance(overrides, dict) or set(overrides) - PACKAGE_REPOSITORIES.keys():
        raise ValueError('Package repository overrides must map known Pyle core names.')
    for repository in overrides.values():
        if not isinstance(repository, str) or not re.fullmatch(r'[\w.-]+/[\w.-]+', repository):
            raise ValueError('Invalid owning GitHub repository.')
    return dict(PACKAGE_REPOSITORIES, **overrides)


def manifests(root):
    names = json.loads(os.environ.get('RESET_COMPOSER_FILES', '["composer.json"]'))
    if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
        raise ValueError('composer-files must be a nonempty JSON array of relative paths.')
    targets = []
    seen = set()
    for name in names:
        relative = Path(name)
        path = (root / relative).resolve()
        if relative.is_absolute() or '..' in relative.parts or not path.is_relative_to(root):
            raise ValueError('Composer manifests must stay within the repository.')
        if path.name != 'composer.json' or path in seen:
            raise ValueError('Use unique composer.json paths.')
        seen.add(path)
        payload = json_object(path)
        packages = []
        for section in ['require', 'require-dev']:
            requirements = payload.get(section, {})
            if not isinstance(requirements, dict):
                raise ValueError(f'{path}: {section} must be an object.')
            packages.extend(name for name in requirements if name in PACKAGE_REPOSITORIES)
        if len(packages) != len(set(packages)):
            raise ValueError(f'{path}: a core package appears in both requirement sections.')
        if packages:
            lock_path = path.with_suffix('.lock')
            original_lock = json_object(lock_path)
            targets.append((path, payload, packages, original_lock))
    return targets


def development_constraints(packages, repositories):
    branches = {}
    constraints = {}
    for package in sorted(packages):
        repository = repositories[package]
        if repository not in branches:
            branch = command(['gh', 'api', f'repos/{repository}', '--jq', '.default_branch'])
            if not branch or branch == 'null':
                raise ValueError(f'No default branch found for {repository}.')
            command(['git', 'check-ref-format', '--branch', branch])
            branches[repository] = branch
        branch = branches[repository]
        numeric_branch = re.fullmatch(r'\d+(?:\.\d+)*(?:\.x)?', branch)
        constraints[package] = branch + '-dev' if numeric_branch else 'dev-' + branch
    return constraints


def restore_constraints(path, payload, constraints):
    changed = False
    for section in ['require', 'require-dev']:
        for package, current in payload.get(section, {}).items():
            if package in constraints and current != constraints[package]:
                print(f'{path}: {package}: {current} -> {constraints[package]}')
                payload[section][package] = constraints[package]
                changed = True
    if changed:
        indentation = re.search(r'\n([ \t]+)"', path.read_text())
        indent = indentation.group(1) if indentation else '    '
        path.write_text(json.dumps(payload, indent=indent, ensure_ascii=False) + '\n')


def locked_packages(lock):
    return {package['name']: (package['version'],
                             package.get('source', {}).get('reference'),
                             package.get('dist', {}).get('reference'))
            for package in lock.get('packages', []) + lock.get('packages-dev', [])}


def refresh_and_validate_lock(path, packages, before, constraints):
    output = command(['composer', 'update', *packages, '--no-install', '--no-scripts',
                      '--no-interaction', '--no-progress'], path.parent)
    print(output)
    command(['composer', 'validate', '--no-check-publish', '--no-interaction'], path.parent)
    previous = locked_packages(before)
    current = locked_packages(json_object(path.with_suffix('.lock')))
    for package in packages:
        if current.get(package, (None,))[0] != constraints[package]:
            raise ValueError(f'{path}: lock does not match the default branch for {package}.')
    unrelated_before = {name: value for name, value in previous.items() if name not in packages}
    unrelated_after = {name: value for name, value in current.items() if name not in packages}
    if unrelated_before != unrelated_after:
        raise ValueError(f'{path}: Composer changed dependencies outside the Pyle target list.')


def report(outcome, commit='', files=None):
    files = files or []
    print(f'Reset outcome: {outcome}' + (f' ({commit})' if commit else ''))
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a') as stream:
            stream.write(f'outcome={outcome}\ncommit-sha={commit}\nchanged-files={json.dumps(files)}\n')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as stream:
            stream.write(f'## Pyle development dependency reset\n\nOutcome: `{outcome}`\n\n')
            if commit:
                stream.write(f'Commit: `{commit}`\n\n')
            for name in files:
                stream.write(f'- `{name}`\n')


def main():
    root = Path(command(['git', 'rev-parse', '--show-toplevel'])).resolve()
    if Path.cwd().resolve() != root:
        raise ValueError('Run the action from the repository root.')
    branch = os.environ.get('RESET_DEVELOPMENT_BRANCH', '')
    if not branch or branch == 'production' or re.fullmatch(r'\d+\.\d+\.x', branch) or branch.startswith('hotfix/'):
        raise ValueError('An explicit development branch is required; release/production refs are not allowed.')
    if command(['git', 'branch', '--show-current']) != branch:
        raise ValueError('The checkout does not match the configured development branch.')
    if command(['git', 'status', '--porcelain']):
        raise ValueError('The repository must have a clean working tree.')
    repositories = package_repositories()
    targets = manifests(root)
    if not targets:
        report('skipped')
        return
    command(['git', 'fetch', 'origin', branch])
    if command(['git', 'rev-parse', 'HEAD']) != command(['git', 'rev-parse', 'FETCH_HEAD']):
        raise ValueError('The development branch moved; rerun on its current head.')
    packages = {package for _, _, names, _ in targets for package in names}
    constraints = development_constraints(packages, repositories)
    allowed = set()
    for path, payload, names, before in targets:
        restore_constraints(path, payload, constraints)
        allowed.update([str(path.relative_to(root)), str(path.with_suffix('.lock').relative_to(root))])
    for path, _, names, before in targets:
        shared_core_packages = packages.intersection(locked_packages(before))
        update_packages = sorted(set(names) | shared_core_packages)
        refresh_and_validate_lock(path, update_packages, before, constraints)
    changed = command(['git', 'diff', '--name-only']).splitlines()
    if not changed:
        report('unchanged')
        return
    if set(changed) - allowed or command(['git', 'ls-files', '--others', '--exclude-standard']):
        raise ValueError('The reset changed unexpected repository files.')
    if os.environ.get('RESET_PUSH', 'true') == 'false':
        report('prepared', files=changed)
        return
    command(['git', 'add', '--', *changed])
    message = os.environ.get('RESET_COMMIT_MESSAGE', 'chore: restore Pyle development dependencies')
    command(['git', '-c', 'user.name=github-actions[bot]',
             '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
             'commit', '-m', message])
    commit = command(['git', 'rev-parse', 'HEAD'])
    command(['git', 'push', 'origin', f'HEAD:refs/heads/{branch}'])
    report('pushed', commit, changed)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        print(f'::error::{error}', file=sys.stderr)
        sys.exit(1)

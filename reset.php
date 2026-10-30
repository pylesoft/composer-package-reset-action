<?php

declare(strict_types=1);

const PACKAGE_REPOSITORIES = [
    'floorbox/cbox-framework' => 'floorbox/cbox-framework',
    'floorbox/cbox-admin-module' => 'floorbox/cbox-admin-module',
    'floorbox/cbox-storefront-module' => 'floorbox/cbox-storefront-module',
    'pylesoft/admin' => 'pylesoft/admin',
    'floorbox/pyle-erp-bridge-module' => 'floorbox/pyle-erp-bridge-module',
];

/** @param list<string> $arguments */
function command(array $arguments, ?string $directory = null): string
{
    $process = proc_open($arguments, [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['redirect', 1]], $pipes, $directory);

    if (! is_resource($process)) {
        throw new RuntimeException("Could not start {$arguments[0]}.");
    }

    $output = stream_get_contents($pipes[1]);
    fclose($pipes[1]);
    $exitCode = proc_close($process);

    if ($exitCode !== 0 || $output === false) {
        throw new RuntimeException("{$arguments[0]} failed:\n{$output}");
    }

    return trim($output);
}

function readJsonObject(string $path): stdClass
{
    if (! is_file($path)) {
        throw new RuntimeException("Missing JSON file: {$path}");
    }

    $contents = file_get_contents($path);

    if ($contents === false) {
        throw new RuntimeException("Could not read {$path}.");
    }

    $payload = json_decode($contents, false, 512, JSON_THROW_ON_ERROR);

    if (! $payload instanceof stdClass) {
        throw new RuntimeException("{$path} must contain a JSON object.");
    }

    return $payload;
}

/** @return array<string, string> */
function packageRepositories(): array
{
    $overrides = json_decode(getenv('RESET_PACKAGE_REPOSITORIES') ?: '{}', false, 512, JSON_THROW_ON_ERROR);

    if (! $overrides instanceof stdClass || array_diff_key(get_object_vars($overrides), PACKAGE_REPOSITORIES) !== []) {
        throw new RuntimeException('Package repository overrides must map known Pyle core names.');
    }

    foreach ($overrides as $repository) {
        if (! is_string($repository) || ! preg_match('/^[\w.-]+\/[\w.-]+$/D', $repository)) {
            throw new RuntimeException('Invalid owning GitHub repository.');
        }
    }

    return array_replace(PACKAGE_REPOSITORIES, get_object_vars($overrides));
}

/** @return list<array{path: string, payload: stdClass, packages: list<string>, before: stdClass}> */
function manifests(string $root): array
{
    $names = json_decode(getenv('RESET_COMPOSER_FILES') ?: '["composer.json"]', false, 512, JSON_THROW_ON_ERROR);

    if (! is_array($names) || $names === []) {
        throw new RuntimeException('composer-files must be a nonempty JSON array of relative paths.');
    }

    $targets = [];
    $seen = [];

    foreach ($names as $name) {
        if (! is_string($name) || str_starts_with($name, '/') || in_array('..', explode('/', $name), true)) {
            throw new RuntimeException('Composer manifests must stay within the repository.');
        }

        $path = realpath("{$root}/{$name}");

        if ($path === false || ! str_starts_with($path, $root.'/') || basename($path) !== 'composer.json' || isset($seen[$path])) {
            throw new RuntimeException('Use unique, existing composer.json paths within the repository.');
        }

        $seen[$path] = true;
        $payload = readJsonObject($path);
        $packages = [];

        foreach (['require', 'require-dev'] as $section) {
            $requirements = property_exists($payload, $section) ? $payload->{$section} : new stdClass;

            if (! $requirements instanceof stdClass) {
                throw new RuntimeException("{$path}: {$section} must be an object.");
            }

            $packages = array_merge($packages, array_keys(array_intersect_key(get_object_vars($requirements), PACKAGE_REPOSITORIES)));
        }

        if (count($packages) !== count(array_unique($packages))) {
            throw new RuntimeException("{$path}: a core package appears in both requirement sections.");
        }

        if ($packages !== []) {
            $targets[] = ['path' => $path, 'payload' => $payload, 'packages' => $packages, 'before' => readJsonObject(dirname($path).'/composer.lock')];
        }
    }

    return $targets;
}

/**
 * @param  list<string>  $packages
 * @param  array<string, string>  $repositories
 * @return array<string, string>
 */
function developmentConstraints(array $packages, array $repositories): array
{
    $branches = [];
    $constraints = [];
    sort($packages);

    foreach ($packages as $package) {
        $repository = $repositories[$package];

        if (! isset($branches[$repository])) {
            $branch = command(['gh', 'api', "repos/{$repository}", '--jq', '.default_branch']);

            if ($branch === '' || $branch === 'null') {
                throw new RuntimeException("No default branch found for {$repository}.");
            }

            command(['git', 'check-ref-format', '--branch', $branch]);
            $branches[$repository] = $branch;
        }

        $branch = $branches[$repository];
        $constraints[$package] = preg_match('/^\d+(?:\.\d+)*(?:\.x)?$/D', $branch) ? $branch.'-dev' : 'dev-'.$branch;
    }

    return $constraints;
}

/** @param array<string, string> $constraints */
function restoreConstraints(string $path, stdClass $payload, array $constraints): void
{
    $changed = false;

    foreach (['require', 'require-dev'] as $section) {
        foreach ($payload->{$section} ?? [] as $package => $current) {
            if (isset($constraints[$package]) && $current !== $constraints[$package]) {
                echo "{$path}: {$package}: {$current} -> {$constraints[$package]}\n";
                $payload->{$section}->{$package} = $constraints[$package];
                $changed = true;
            }
        }
    }

    if (! $changed) {
        return;
    }

    preg_match('/\n([ \t]+)"/', file_get_contents($path), $indentation);
    $indent = $indentation[1] ?? '    ';
    $json = json_encode($payload, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    $json = preg_replace_callback('/^( +)/m', fn (array $match): string => str_repeat($indent, intdiv(strlen($match[1]), 4)), $json);

    if (file_put_contents($path, $json."\n") === false) {
        throw new RuntimeException("Could not write {$path}.");
    }
}

/** @return array<string, array{string, ?string, ?string}> */
function lockedPackages(stdClass $lock): array
{
    $packages = [];

    foreach (array_merge($lock->packages ?? [], $lock->{'packages-dev'} ?? []) as $package) {
        $packages[$package->name] = [$package->version, $package->source->reference ?? null, $package->dist->reference ?? null];
    }

    ksort($packages);

    return $packages;
}

/**
 * @param  list<string>  $packages
 * @param  array<string, string>  $constraints
 */
function refreshAndValidateLock(string $path, array $packages, stdClass $before, array $constraints): void
{
    echo command(['composer', 'update', ...$packages, '--no-install', '--no-scripts', '--no-interaction', '--no-progress'], dirname($path))."\n";
    command(['composer', 'validate', '--no-check-publish', '--no-interaction'], dirname($path));
    $previous = lockedPackages($before);
    $current = lockedPackages(readJsonObject(dirname($path).'/composer.lock'));

    foreach ($packages as $package) {
        if (($current[$package][0] ?? null) !== $constraints[$package]) {
            throw new RuntimeException("{$path}: lock does not match the default branch for {$package}.");
        }
    }

    $targetKeys = array_fill_keys($packages, true);

    if (array_diff_key($previous, $targetKeys) !== array_diff_key($current, $targetKeys)) {
        throw new RuntimeException("{$path}: Composer changed dependencies outside the Pyle target list.");
    }
}

/** @param list<string> $files */
function report(string $outcome, string $commit = '', array $files = []): void
{
    echo "Reset outcome: {$outcome}".($commit !== '' ? " ({$commit})" : '')."\n";

    if ($output = getenv('GITHUB_OUTPUT')) {
        file_put_contents($output, "outcome={$outcome}\ncommit-sha={$commit}\nchanged-files=".json_encode($files, JSON_THROW_ON_ERROR)."\n", FILE_APPEND);
    }

    if ($summary = getenv('GITHUB_STEP_SUMMARY')) {
        $content = "## Pyle development dependency reset\n\nOutcome: `{$outcome}`\n\n";
        $content .= $commit !== '' ? "Commit: `{$commit}`\n\n" : '';

        foreach ($files as $file) {
            $content .= "- `{$file}`\n";
        }

        file_put_contents($summary, $content, FILE_APPEND);
    }
}

function resetDependencies(): void
{
    $root = realpath(command(['git', 'rev-parse', '--show-toplevel']));

    if (realpath(getcwd()) !== $root) {
        throw new RuntimeException('Run the action from the repository root.');
    }

    $branch = getenv('RESET_DEVELOPMENT_BRANCH') ?: '';

    if ($branch === '' || $branch === 'production' || preg_match('/^\d+\.\d+\.x$/D', $branch) || str_starts_with($branch, 'hotfix/')) {
        throw new RuntimeException('An explicit development branch is required; release/production refs are not allowed.');
    }

    if (command(['git', 'branch', '--show-current']) !== $branch || command(['git', 'status', '--porcelain']) !== '') {
        throw new RuntimeException('Check out the configured development branch with a clean working tree.');
    }

    $repositories = packageRepositories();
    $targets = manifests($root);

    if ($targets === []) {
        report('skipped');

        return;
    }

    command(['git', 'fetch', 'origin', $branch]);

    if (command(['git', 'rev-parse', 'HEAD']) !== command(['git', 'rev-parse', 'FETCH_HEAD'])) {
        throw new RuntimeException('The development branch moved; rerun on its current head.');
    }

    $packages = array_values(array_unique(array_merge(...array_column($targets, 'packages'))));
    $constraints = developmentConstraints($packages, $repositories);
    $allowedFiles = [];

    foreach ($targets as $target) {
        restoreConstraints($target['path'], $target['payload'], $constraints);
        $allowedFiles[] = substr($target['path'], strlen($root) + 1);
        $allowedFiles[] = substr(dirname($target['path']).'/composer.lock', strlen($root) + 1);
    }

    foreach ($targets as $target) {
        $sharedCorePackages = array_intersect($packages, array_keys(lockedPackages($target['before'])));
        $updatePackages = array_values(array_unique(array_merge($target['packages'], $sharedCorePackages)));
        sort($updatePackages);
        refreshAndValidateLock($target['path'], $updatePackages, $target['before'], $constraints);
    }

    $diff = command(['git', 'diff', '--name-only']);

    if ($diff === '') {
        report('unchanged');

        return;
    }

    $changedFiles = explode("\n", $diff);

    if (array_diff($changedFiles, $allowedFiles) !== [] || command(['git', 'ls-files', '--others', '--exclude-standard']) !== '') {
        throw new RuntimeException('The reset changed unexpected repository files.');
    }

    if (getenv('RESET_PUSH') === 'false') {
        report('prepared', files: $changedFiles);

        return;
    }

    command(['git', 'add', '--', ...$changedFiles]);
    $message = getenv('RESET_COMMIT_MESSAGE') ?: 'chore: restore Pyle development dependencies';
    command(['git', '-c', 'user.name=github-actions[bot]', '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com', 'commit', '-m', $message]);
    $commit = command(['git', 'rev-parse', 'HEAD']);
    command(['git', 'push', 'origin', "HEAD:refs/heads/{$branch}"]);
    report('pushed', $commit, $changedFiles);
}

try {
    resetDependencies();
} catch (RuntimeException|JsonException $error) {
    fwrite(STDERR, "::error::{$error->getMessage()}\n");
    exit(1);
}

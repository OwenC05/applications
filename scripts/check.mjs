import { readdirSync } from 'node:fs';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
const roots = ['src', 'public', 'test', 'scripts', 'backend/ui'];
let checked = 0;
for (const root of roots) {
  for (const entry of readdirSync(root, { recursive: true, withFileTypes: true })) {
    if (!entry.isFile() || !/\.(?:mjs|js)$/.test(entry.name)) continue;
    const file = join(entry.parentPath ?? entry.path, entry.name);
    const result = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8', windowsHide: true });
    if (result.status !== 0) { process.stderr.write(result.stderr || `Syntax check failed: ${file}\n`); process.exit(1); }
    checked++;
  }
}
console.log(`Syntax checked ${checked} JavaScript modules. No external lint/typecheck dependencies are installed.`);

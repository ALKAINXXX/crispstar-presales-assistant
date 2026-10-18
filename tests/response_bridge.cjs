// Actual controlled Orchestrator/domain/SQLite results supplied by pytest are
// mapped through the same React Thread used by the production UI.
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const frontend = path.resolve(__dirname, '../frontend');
const result = spawnSync(process.execPath, [path.join(frontend, 'node_modules/vitest/vitest.mjs'), 'run', 'src/business/responseBridge.test.tsx'], { cwd: frontend, encoding: 'utf8', env: { ...process.env, RESPONSE_BRIDGE_FIXTURE: path.resolve(process.argv[2]) } });
process.stdout.write(result.stdout ?? '');
process.stderr.write(result.stderr ?? '');
if (result.error) process.stderr.write(String(result.error));
process.exitCode = result.status ?? 1;

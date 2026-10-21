// Entry point: load the environment (fail closed), build the app, listen, shut down cleanly.
import { buildApp } from './app.js';
import { loadConfig } from './config.js';
import { listenWithFallback } from './listen.js';
import { installShutdown } from './shutdown.js';

async function main(): Promise<void> {
  const config = loadConfig();
  const app = await buildApp({ config });
  installShutdown(app, { timeoutMs: config.shutdownTimeoutMs });
  const host = await listenWithFallback(app, { host: config.host, port: config.port });
  app.log.info(
    { host, port: config.port, authMode: config.authMode, origins: config.webOrigins, nodeEnv: config.nodeEnv, trustProxy: config.trustProxy },
    'marden api listening',
  );
}

main().catch((err: unknown) => {
  // Config errors name the variable, never its value.
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});

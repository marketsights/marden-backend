// Entry point: load the environment (fail closed), build the app, listen, shut down cleanly.
import { buildApp } from './app.js';
import { loadConfig } from './config.js';

async function main(): Promise<void> {
  const config = loadConfig();
  const app = await buildApp({ config });
  const shutdown = (signal: string): void => {
    app.log.info({ signal }, 'shutting down');
    app.close().then(
      () => process.exit(0),
      (err: unknown) => {
        app.log.error({ err }, 'shutdown failed');
        process.exit(1);
      },
    );
  };
  process.once('SIGTERM', () => shutdown('SIGTERM'));
  process.once('SIGINT', () => shutdown('SIGINT'));
  await app.listen({ host: config.host, port: config.port });
  app.log.info({ authMode: config.authMode, origins: config.webOrigins }, 'marden api listening');
}

main().catch((err: unknown) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});

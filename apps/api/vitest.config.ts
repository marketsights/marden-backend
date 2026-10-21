import { defineConfig } from 'vitest/config';

// Tests run against the local Postgres in a throwaway schema created by test/global-setup.ts.
// Files run one at a time because several of them use the shared dev org.
export default defineConfig({
  test: {
    include: ['test/**/*.test.ts'],
    globalSetup: ['test/global-setup.ts'],
    fileParallelism: false,
    testTimeout: 20_000,
    hookTimeout: 30_000,
  },
});

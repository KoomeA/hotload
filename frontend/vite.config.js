import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [
    sveltekit({
      adapter: adapter({ pages: 'build', assets: 'build', precompress: false, strict: true }),
      // Prerendered pages get a CSP <meta> whose script hashes are computed at build time.
      csp: {
        mode: 'hash',
        directives: {
          'default-src': ['self'],
          'script-src': ['self'],
          'style-src': ['self', 'unsafe-inline'],
          'img-src': ['self', 'data:'], // thumbnails come through our own /api/v1/thumb, never straight from third-party hosts
          'connect-src': ['self'],
          'base-uri': ['self'],
          'form-action': ['self'],
          'object-src': ['none']
        }
      }
    })
  ],
  server: { proxy: { '/api': 'http://localhost:8000', '/healthz': 'http://localhost:8000' } },
  test: { include: ['src/**/*.test.js'], environment: 'node' }
});

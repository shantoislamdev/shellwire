import fs from 'fs';
import { defineConfig } from 'astro/config';
import mdx from '@astrojs/mdx';
import icon from 'astro-icon';
import tailwindcjs from '@tailwindcss/vite';

const vesperTheme = JSON.parse(fs.readFileSync(new URL('./src/vesper-theme.json', import.meta.url), 'utf8'));
const vesperLightTheme = JSON.parse(fs.readFileSync(new URL('./src/vesper-light-theme.json', import.meta.url), 'utf8'));

// https://astro.build/config
export default defineConfig({
  site: 'https://agenticwebview.example.com',
  integrations: [mdx(), icon()],
  prefetch: { prefetchAll: true },
  vite: {
    plugins: [tailwindcjs()]
  },
  server: {
    port: 3000,
    host: '0.0.0.0'
  },
  markdown: {
    shikiConfig: {
      themes: {
        light: vesperLightTheme,
        dark: vesperTheme,
      },
      defaultColor: false,
    }
  }
});

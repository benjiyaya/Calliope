import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	kit: {
		adapter: adapter({
			fallback: 'index.html',
		}),
		paths: {
			base: '',
			// Absolute /_app/... URLs so a document load of /project/12 still
			// finds the bundle. adapter-static otherwise emits ./_app on prerendered pages.
			relative: false,
		},
		prerender: {
			handleHttpError: ({ path, message }) => {
				if (path === '/favicon.png') return;
				throw new Error(message);
			},
		},
	},
};

export default config;

import { redirect } from '@sveltejs/kit';

export const prerender = false;
export const ssr = false;

export function load() {
	redirect(302, '/agents');
}

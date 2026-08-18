import { error } from '@sveltejs/kit';

export const prerender = true;
export const ssr = false;

export function load() {
	return {};
}

import type { HandleClientError } from '@sveltejs/kit';
import { t } from '$lib/i18n.svelte';

export const handleError: HandleClientError = ({ error }) => {
	console.error(error);
	return {
		message: t('global.somethingWentWrong'),
	};
};

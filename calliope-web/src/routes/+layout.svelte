<script lang="ts">
	import './app.css';
	import { QueryClient, QueryClientProvider } from '@tanstack/svelte-query';
	import ToastHost from '$lib/components/ToastHost.svelte';

	const queryClient = new QueryClient({
		defaultOptions: {
			queries: { refetchOnWindowFocus: false, retry: 1 },
		},
	});
</script>

<QueryClientProvider client={queryClient}>
	<slot />
	<ToastHost />
</QueryClientProvider>

// This ingress holds no bot credentials, decryption key or database binding.
// The existing Worker authenticates and processes requests through an internal
// service binding, so its public workers.dev DNS is not needed.
export const onRequest: PagesFunction<{ BOT: Fetcher }> = context => context.env.BOT.fetch(context.request);

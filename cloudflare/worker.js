export default {
  async fetch() {
    return new Response('ok');
  },
  async scheduled(event, env) {
    const resp = await fetch('https://api.github.com/repos/Zhang-hao111/daily-news-email/actions/workflows/daily.yml/dispatches', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.GH_TOKEN}`,
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'User-Agent': 'daily-news-trigger'
      },
      body: JSON.stringify({ ref: 'main', inputs: { source: 'cloudflare-worker' } })
    });
    console.log('dispatch status:', resp.status);
  }
};

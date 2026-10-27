export default {
  async scheduled(event, env, ctx) {
    const url = "https://quizbot-s482.onrender.com/health";
    try {
      const res = await fetch(url, { method: "GET" });
      console.log(`[keepalive] ping ${url} -> ${res.status}`);
    } catch (err) {
      console.log(`[keepalive] ping failed: ${err}`);
    }
  },
  async fetch() {
    return new Response("quizbot keepalive worker ok");
  }
};

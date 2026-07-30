const worker = {
  async fetch(request, env) {
    const url = new URL(request.url)
    const response = await env.ASSETS.fetch(request)

    if (
      response.status === 404 &&
      request.method === 'GET' &&
      request.headers.get('accept')?.includes('text/html')
    ) {
      return env.ASSETS.fetch(new Request(new URL('/index.html', url), request))
    }

    return response
  },
}

export default worker

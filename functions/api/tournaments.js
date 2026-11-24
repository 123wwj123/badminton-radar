export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  
  const city = url.searchParams.get('city') || '';
  const status = url.searchParams.get('status') || '';
  const keyword = url.searchParams.get('q') || '';

  try {
    let query = `SELECT * FROM tournaments WHERE 1=1`;
    const params = [];

    if (city && city !== '全部') {
      query += ` AND city = ?`;
      params.push(city);
    }

    if (status && status !== 'all') {
      query += ` AND status = ?`;
      params.push(status);
    }

    if (keyword) {
      query += ` AND (title LIKE ? OR venue LIKE ? OR province LIKE ? OR city LIKE ?)`;
      const likeKey = `%${keyword}%`;
      params.push(likeKey, likeKey, likeKey, likeKey);
    }

    query += ` ORDER BY match_date ASC LIMIT 200`;

    const [eventsRes, citiesRes] = await Promise.all([
      env.DB.prepare(query).bind(...params).all(),
      env.DB.prepare(`SELECT DISTINCT city FROM tournaments WHERE city IS NOT NULL AND city != '' ORDER BY city ASC`).all()
    ]);

    const cities = (citiesRes.results || []).map(row => row.city);

    return new Response(JSON.stringify({
      success: true,
      data: eventsRes.results,
      cities
    }), {
      headers: { 'Content-Type': 'application/json; charset=utf-8' }
    });
  } catch (err) {
    return new Response(JSON.stringify({ success: false, error: err.message }), {
      status: 500,
      headers: { 'Content-Type': 'application/json; charset=utf-8' }
    });
  }
}

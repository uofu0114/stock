// 日付ページ（/stock/YYYY-MM-DD/）を、テンプレートとその日の素材から組み立てて表示する
(async function () {
  const m = location.pathname.match(/(\d{4}-\d{2}-\d{2})/);
  if (!m) { document.body.textContent = '日付が見つかりません'; return; }
  const d = m[1];
  const root = new URL('../', location.href).href;
  const get = async (p) => { const r = await fetch(root + p, { cache: 'no-cache' }); if (!r.ok) throw new Error(p + ' ' + r.status); return r.text(); };
  try {
    const [tpl, mid, data, metaTxt] = await Promise.all([
      get('tools/template.html'), get('daily/' + d + '/mid.html'), get('daily/' + d + '/data.txt'), get('daily/' + d + '/meta.json')
    ]);
    const meta = JSON.parse(metaTxt);
    const [, mo, da] = d.split('-');
    const rep = {
      '__MID__': mid,
      '__HEADLINE__': meta.headline,
      '__SHORTDATE__': (+mo) + '/' + (+da),
      '__DATA__': data.trim(),
      '__C8306__': meta.charts['8306'].closes,
      '__C7685__': meta.charts['7685'].closes,
      '__L8306__': JSON.stringify(meta.charts['8306'].lines),
      '__L7685__': JSON.stringify(meta.charts['7685'].lines)
    };
    let t = tpl;
    for (const k in rep) t = t.split(k).join(rep[k]);
    const nav = '<p class="note" style="max-width:1180px;margin:12px auto 0;padding-inline:16px"><a href="../">← 過去の日付一覧</a></p>';
    t = t.replace('<div class="wrap">', '</head><body>' + nav + '<div class="wrap">');
    const html = '<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><meta name="robots" content="noindex">' + t + '</body></html>';
    document.open(); document.write(html); document.close();
  } catch (e) {
    document.body.innerHTML = '<p style="font-family:sans-serif;padding:16px">ページを読み込めませんでした（' + String(e.message).replace(/</g, '&lt;') + '）。<a href="../">日付一覧へ</a></p>';
  }
})();

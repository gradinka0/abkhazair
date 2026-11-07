"""Bundle the browsable site in one offline HTML. No form submits to another origin."""
import base64
import re
from pathlib import Path

root=Path(__file__).resolve().parent
public=root/'public'
page=(public/'index.html').read_text(encoding='utf-8')
page=re.sub(r'<link rel="stylesheet" href="styles\.css(?:\?[^"]*)?">',lambda _: '<style>\n'+(public/'styles.css').read_text(encoding='utf-8')+'\n</style>',page)
page=re.sub(r'<script src="(?:translations|app)\.js(?:\?[^"]*)?" defer></script>','',page)
for filename, mime in [('airport.png','image/png'),('abkhaz-air-logo.png','image/png'),('favicon.svg','image/svg+xml')]:
    source='data:'+mime+';base64,'+base64.b64encode((public/'assets'/filename).read_bytes()).decode('ascii')
    page=page.replace('assets/'+filename,source)
page=page.replace('href="admin.html"','href="#team" data-info="offline"')
scripts='\n<script>window.AIR_STANDALONE=true;</script>\n'
for name in ('translations.js','app.js'):
    scripts+='<script>\n'+(public/name).read_text(encoding='utf-8').replace('</script','<\\/script')+'\n</script>\n'
page=page.replace('</body>',scripts+'</body>')
(root/'preview.html').write_text(page,encoding='utf-8')
print('preview.html created')

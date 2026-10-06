"""Real-browser regression: run against a local or deployed Dayplan URL."""
import json
import subprocess
import sys

URL = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:18787'
SESSION = 'dayplan-project-overflow-test'

def browser(*args):
    result = json.loads(subprocess.check_output(['agent-browser', '--session', SESSION, '--json', *args], text=True))
    assert result['success'], result.get('error')
    return result.get('data', {}).get('result')

try:
    browser('open', URL)
    for width in (320, 390, 768, 1440):
        browser('set', 'viewport', str(width), '844')
        result = browser('eval', '''(() => {
          const project = 'Open Path Webdev Requests › Wellness Clones Shutdown '.repeat(8);
          const list = document.querySelector('#task-list');
          list.replaceChildren();
          for (const featured of [false, true]) {
            const card = buildCard({id:'fixture', source:'asana', title:'Project overflow regression fixture', project, tags:[], pinned:true, zone:'ordered', priority:0}, 1, featured);
            list.appendChild(card);
          }
          return [...list.children].map(card => {
            const badge = [...card.querySelectorAll('.meta .badge')].find(x => x.textContent === project);
            const rect = badge.getBoundingClientRect(), meta = card.querySelector('.meta').getBoundingClientRect();
            const style = getComputedStyle(badge);
            return {right:rect.right, metaRight:meta.right, width:rect.width, scroll:badge.scrollWidth, client:badge.clientWidth, ellipsis:style.textOverflow, hidden:style.overflowX, textPreserved:badge.textContent === project};
          });
        })()''')
        print(json.dumps({'viewport':width, 'cards':result}))
        for card in result:
            assert card['right'] <= card['metaRight'] + 1, f'Project escaped metadata at {width}px'
            assert card['ellipsis'] == 'ellipsis' and card['hidden'] == 'hidden', f'Missing ellipsis at {width}px'
            assert card['scroll'] > card['client'] and card['textPreserved']
finally:
    browser('close')

from playwright.sync_api import sync_playwright
from pathlib import Path
import json
out=Path(__file__).parent
results=[]
with sync_playwright() as p:
 for attempt in [1,2]:
  b=p.chromium.launch(headless=True);c=b.new_context(viewport={'width':1440,'height':1000},record_video_dir=str(out));c.tracing.start(screenshots=True,snapshots=True)
  page=c.new_page();page.goto('https://excalidraw.com');page.wait_for_timeout(1800);page.get_by_test_id('main-menu-trigger').click();page.wait_for_timeout(400)
  snapshots=[]
  for width,name in [(1440,'desktop'),(800,'tablet'),(1440,'restored')]:
   page.set_viewport_size({'width':width,'height':1000});page.wait_for_timeout(600)
   menu=page.get_by_role('menu');trigger=menu.get_by_role('button',name='Canvas background',exact=True)
   snapshots.append({'viewport_width':width,'name':name,'swatch_count':menu.locator('button.color-picker__button:not(.properties-trigger)').count(),'menu_box':menu.bounding_box(),'picker_box':trigger.bounding_box()})
   page.screenshot(path=str(out/f'{attempt}-{name}.png'));page.wait_for_timeout(500)
  c.tracing.stop(path=str(out/f'trace-{attempt}.zip'));video=page.video;c.close();video.save_as(str(out/f'replay-{attempt}.webm'));b.close()
  result={'attempt':attempt,'url':'https://excalidraw.com','browser':'Playwright Chromium','version':'live deployment, commit unknown','snapshots':snapshots,'reproduced':snapshots[0]['swatch_count']==5 and snapshots[1]['swatch_count']==0 and snapshots[2]['swatch_count']==5}
  results.append(result);(out/'result.json').write_text(json.dumps(results,indent=2));print(json.dumps(result),flush=True)

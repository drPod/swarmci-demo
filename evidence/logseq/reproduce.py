import asyncio,json
from pathlib import Path
from playwright.async_api import async_playwright
OUT=Path(__file__).resolve().parent
original='Cycle through open windows of the same application (<kbd>Alt</kbd> + <kbd>`</kbd>)'
async def main():
 async with async_playwright() as p:
  b=await p.chromium.launch()
  results=[]
  for name,line in [('original',original),('control',original.replace('(<kbd>','( <kbd>'))]:
   c=await b.new_context(viewport={'width':1440,'height':1000},record_video_dir=str(OUT/'video'))
   page=await c.new_page()
   page.set_default_timeout(15000)
   try:
    await page.goto('https://demo.logseq.com',wait_until='domcontentloaded',timeout=25000)
    await page.get_by_text('Hi, welcome to Logseq!',exact=True).click()
    await page.locator('textarea:visible').fill(line)
    await page.screenshot(path=str(OUT/(name+'-editing.png')))
    await page.get_by_text("This is a 3 minute tutorial on how to use Logseq. Let's get started!",exact=True).click()
    await page.wait_for_timeout(800)
    await page.screenshot(path=str(OUT/(name+'-rendered.png')))
    block=page.locator('.block-content').filter(has_text='Cycle through open windows').first
    results.append({'case':name,'input':line,'text':await block.inner_text(),'html':await block.inner_html(),'kbd_texts':await block.locator('kbd').all_text_contents()})
   except Exception as e: results.append({'case':name,'error':str(e)})
   await c.close()
  (OUT/'measurements.json').write_text(json.dumps({'browser_version':b.version,'url':'https://demo.logseq.com/','cases':results},indent=2))
  print(json.dumps(results,indent=2))
  await b.close()
asyncio.run(main())

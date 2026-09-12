import asyncio,json
from pathlib import Path
from playwright.async_api import async_playwright
P=Path(__file__).parent
async def main():
 async with async_playwright() as w:
  b=await w.chromium.launch()
  c=await b.new_context(viewport={'width':1440,'height':1000},record_video_dir=str(P))
  page=await c.new_page()
  await page.goto('https://www.tldraw.com',wait_until='domcontentloaded')
  await page.wait_for_timeout(4000)
  await page.get_by_test_id('tools.rectangle').click()
  await page.mouse.move(300,200);await page.mouse.down();await page.mouse.move(700,600,steps=20);await page.mouse.up()
  await page.get_by_test_id('tools.note').click();await page.mouse.click(500,400)
  await page.keyboard.press('Escape');await page.keyboard.press('v');await page.keyboard.press('Meta+a')
  await page.wait_for_timeout(500)
  async def capture(name):
   await page.screenshot(path=str(P/(name+'.png')))
   data=await page.locator('.tl-shape').evaluate_all('(els)=>els.map(e=>({id:e.getAttribute("data-shape-id"),type:e.getAttribute("data-shape-type"),rect:(()=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})(),html:e.outerHTML.slice(0,500)}))')
   print(name,json.dumps(data),flush=True)
   (P/(name+'.json')).write_text(json.dumps(data,indent=2))
  await capture('ungrouped-before')
  await page.mouse.move(700,600);await page.mouse.down();await page.mouse.move(900,800,steps=30);await page.mouse.up();await page.wait_for_timeout(400)
  await capture('ungrouped-after')
  await page.keyboard.press('Meta+z');await page.keyboard.press('Meta+g');await page.wait_for_timeout(400)
  await capture('grouped-before')
  print('selection',await page.locator('[data-testid]').evaluate_all('(els)=>els.filter(e=>/handle|selection/.test(e.dataset.testid)).map(e=>({id:e.dataset.testid,html:e.outerHTML.slice(0,300)}))'),flush=True)
  await page.mouse.move(700,600);await page.mouse.down();await page.mouse.move(900,800,steps=30);await page.mouse.up();await page.wait_for_timeout(500)
  await capture('grouped-after')
  await c.close();await b.close()
asyncio.run(main())

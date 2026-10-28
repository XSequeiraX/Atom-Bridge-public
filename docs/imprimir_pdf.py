import asyncio, sys
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path='/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
        pg = await b.new_page()
        await pg.goto("file://" + sys.argv[1]); await pg.wait_for_timeout(500)
        await pg.pdf(path=sys.argv[2], landscape=True, format="A4", print_background=True, prefer_css_page_size=True)
        await b.close()
asyncio.run(main())

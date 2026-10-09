"""Read one live listing page per IBBI order category; never download PDFs."""
import httpx
from bs4 import BeautifulSoup
from backend.automatic_orders import DEFAULT_CATEGORIES
from backend.ibbi_orders import BASE, parse_listing
from backend.url_import import fetch


def main():
    with httpx.Client(timeout=30,follow_redirects=False,trust_env=False) as client:
        for category in DEFAULT_CATEGORIES:
            content,url=fetch(client,f'{BASE}/orders/{category}',15*1024*1024)
            rows,pages,_=parse_listing(content.decode('utf-8',errors='replace'),allow_empty=True)
            print(f'{category}: {len(rows)} PDFs on first page, {pages} listing pages',flush=True)
            if not rows:
                soup=BeautifulSoup(content,'html.parser')
                print([(a.get('href'),a.get_text(' ',strip=True)[:100]) for a in soup.select('table.reporttable a[href]')[:5]],flush=True)


if __name__=='__main__':
    main()

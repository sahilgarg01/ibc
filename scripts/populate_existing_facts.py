"""Fill missing facts in existing draft PDFs; retain reviewed/entered values."""
from fastapi.testclient import TestClient
from backend.main import app


def main():
    processed=0
    failed=0
    with TestClient(app) as client:
        cases=client.get('/api/cases?status=draft')
        cases.raise_for_status()
        for case in cases.json():
            detail=client.get('/api/cases/'+case['id']).json()
            for doc in detail['documents']:
                response=client.post('/api/documents/'+doc['id']+'/extract-facts')
                if response.is_success:
                    processed+=1
                else:
                    failed+=1
                    print(f'Document {doc["id"]}: extraction failed ({response.status_code})',flush=True)
                if (processed+failed)%10==0:
                    print(f'Processed {processed} documents; {failed} failures',flush=True)
        print(f'Available facts extracted for {processed} draft documents; {failed} failures; approved records preserved.')


if __name__=='__main__':
    main()

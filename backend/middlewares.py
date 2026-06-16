from starlette.middleware.cors import CORSMiddleware

from app import app
from config import CORS_ALLOW_ORIGINS, CORS_ALLOW_CREDENTIALS, CORS_ALLOW_METHODS, CORS_ALLOW_HEADERS, \
    CORS_EXPOSE_HEADERS

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=CORS_ALLOW_CREDENTIALS,
    allow_methods=CORS_ALLOW_METHODS,
    allow_headers=CORS_ALLOW_HEADERS,
    expose_headers=CORS_EXPOSE_HEADERS
)

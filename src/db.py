import os

from dotenv import load_dotenv
from sqlalchemy import create_engine

load_dotenv()


def get_engine():
    url = os.environ["DATABASE_URL"]
    return create_engine(url)

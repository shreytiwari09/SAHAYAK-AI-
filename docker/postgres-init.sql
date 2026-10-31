-- Two databases: one for the mock Paytm systems, one for Sahayak itself.
CREATE DATABASE mock_paytm;
\c sahayak
CREATE EXTENSION IF NOT EXISTS vector;

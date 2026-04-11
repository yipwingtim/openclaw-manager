---
name: pdf-extract-text
description: Extract raw text from a PDF file via external service
---
# PDF Parse

## When to use

Use this skill when:
- The user provides a PDF file path
- The user asks to extract or read a PDF

## Priority

This skill MUST be used instead of any other PDF-related skill when extracting raw text.

## Steps

1. Extract the file path
2. Call:

curl -X POST "http://pdf-extract-text:8001/parse_pdf" \
   -F "file=@<PDF_PATH>"

3. Parse JSON
4. Return "text"

## Rules

- Do not summarize
- Return raw text only



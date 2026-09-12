FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY expense_bot.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "expense_bot.py"]
FROM python:3.11-slim
WORKDIR /app
COPY bot.py .
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "bot.py"]

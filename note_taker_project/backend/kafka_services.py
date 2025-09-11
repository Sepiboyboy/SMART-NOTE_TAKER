
from kafka import KafkaProducer, KafkaConsumer
from pymongo import MongoClient
import openai
import json
import threading
import os

# Load OpenAI API key
openai.api_key = os.getenv("OPENAI_API_KEY")

# Kafka Producer
producer = KafkaProducer(
    bootstrap_servers='kafka:9092',
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

# MongoDB setup
mongo_client = MongoClient("mongodb://mongodb:27017")
db = mongo_client["notetaker"]
collection = db["summaries"]

# Send data to Kafka topic
def send_to_kafka(topic: str, data: dict):
    producer.send(topic, value=data)
    producer.flush()

# Kafka consumer thread
def kafka_consumer_worker():
    consumer = KafkaConsumer(
        'translated-text',
        bootstrap_servers='kafka:9092',
        value_deserializer=lambda m: json.loads(m.decode('utf-8')),
        auto_offset_reset='earliest',
        group_id='summarizer-group'
    )

    for message in consumer:
        note_id = message.value.get("note_id")
        translated_text = message.value.get("text")

        try:
            response = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that summarizes text."},
                    {"role": "user", "content": f"Summarize the following note:\n\n{translated_text}"}
                ]
            )
            summary = response['choices'][0]['message']['content']
        except Exception as e:
            summary = f"[Error generating summary]: {str(e)}"

        collection.insert_one({"note_id": note_id, "summary": summary})

threading.Thread(target=kafka_consumer_worker, daemon=True).start()

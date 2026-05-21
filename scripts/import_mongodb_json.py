import argparse
import os
from pathlib import Path

from bson import json_util
from pymongo import MongoClient, ReplaceOne


def collection_name_from_file(path: Path, database_name: str) -> str:
    prefix = f"{database_name}."
    name = path.stem
    if name.startswith(prefix):
        return name[len(prefix):]
    return name


def load_documents(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as file:
        data = json_util.loads(file.read())

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]

    raise ValueError(f"{path} does not contain a JSON document or array")


def import_collection(db, collection_name: str, documents: list[dict], drop: bool) -> int:
    collection = db[collection_name]

    if drop:
        collection.drop()

    if not documents:
        return 0

    operations = []
    plain_documents = []

    for document in documents:
        if "_id" in document:
            operations.append(
                ReplaceOne({"_id": document["_id"]}, document, upsert=True)
            )
        else:
            plain_documents.append(document)

    imported_count = 0

    if operations:
        result = collection.bulk_write(operations, ordered=False)
        imported_count += result.upserted_count + result.modified_count

    if plain_documents:
        result = collection.insert_many(plain_documents, ordered=False)
        imported_count += len(result.inserted_ids)

    return imported_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import MongoDB Extended JSON files into an Attentify database."
    )
    parser.add_argument(
        "folder",
        help="Folder containing files like attentify.users.json",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("DB_NAME", "attentify"),
        help="MongoDB database name. Defaults to DB_NAME or attentify.",
    )
    parser.add_argument(
        "--uri",
        default=os.getenv("MONGO_URL"),
        help="MongoDB connection string. Defaults to MONGO_URL.",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="Drop each target collection before importing it.",
    )
    args = parser.parse_args()

    if not args.uri:
        raise SystemExit("Missing MongoDB URI. Pass --uri or set MONGO_URL.")

    folder = Path(args.folder)
    if not folder.is_dir():
        raise SystemExit(f"Folder not found: {folder}")

    files = sorted(folder.glob(f"{args.db}.*.json"))
    if not files:
        files = sorted(folder.glob("*.json"))

    if not files:
        raise SystemExit(f"No JSON files found in {folder}")

    client = MongoClient(args.uri)
    db = client[args.db]

    for file_path in files:
        collection_name = collection_name_from_file(file_path, args.db)
        documents = load_documents(file_path)
        imported_count = import_collection(db, collection_name, documents, args.drop)
        print(f"{collection_name}: imported {imported_count} documents")


if __name__ == "__main__":
    main()

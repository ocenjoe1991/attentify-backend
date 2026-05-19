from bson import ObjectId
from pydantic_core import core_schema
from pydantic import GetJsonSchemaHandler

class PyObjectId(ObjectId):
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        return core_schema.no_info_plain_validator_function(cls.validate)

    @classmethod
    def validate(cls, value):
        if isinstance(value, ObjectId):
            return value
        if not ObjectId.is_valid(value):
            raise ValueError("Invalid ObjectId")
        return ObjectId(value)

    @classmethod
    def __get_pydantic_json_schema__(cls, schema, handler: GetJsonSchemaHandler):
        return {"type": "string", "example": "60dbf3b82f8fb814c8c10a2b"}

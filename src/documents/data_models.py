import dataclasses
import datetime
import enum
from pathlib import Path
from typing import Dict
from typing import List
from typing import Optional

import magic
from dateutil.parser import isoparse


@dataclasses.dataclass
class DocumentMetadataOverrides:
    """
    Manages overrides for document fields which normally would
    be set from content or matching.  All fields default to None,
    meaning no override is happening
    """

    filename: Optional[str] = None
    title: Optional[str] = None
    correspondent_id: Optional[int] = None
    document_type_id: Optional[int] = None
    tag_ids: Optional[List[int]] = None
    created: Optional[datetime.datetime] = None
    asn: Optional[int] = None
    owner_id: Optional[int] = None

    def as_dict(self) -> Dict:
        return {
            "filename": self.filename,
            "title": self.title,
            "correspondent_id": self.correspondent_id,
            "document_type_id": self.document_type_id,
            "archive_serial_num": self.asn,
            "tag_ids": self.tag_ids,
            "created": self.created.isoformat() if self.created else None,
            "owner_id": self.owner_id,
        }

    @staticmethod
    def from_dict(data: Optional[Dict]) -> "DocumentMetadataOverrides":
        if data is None:
            return DocumentMetadataOverrides()
        return DocumentMetadataOverrides(
            data["filename"],
            data["title"],
            data["correspondent_id"],
            data["document_type_id"],
            data["tag_ids"],
            isoparse(data["created"]) if data["created"] else None,
            data["archive_serial_num"],
            data["owner_id"],
        )


class DocumentSource(enum.IntEnum):
    """
    The source of an incoming document.  May have other uses in the future
    """

    CONSUME_FOLDER = enum.auto()
    API_UPLOAD = enum.auto()
    MAIL_FETCH = enum.auto()


@dataclasses.dataclass
class ConsumableDocument:
    """
    Encapsulates an incoming document, either from consume folder, API upload
    or mail fetching and certain useful operations on it.
    """

    source: DocumentSource
    original_file: Path
    mime_type: Optional[str] = None

    def __post_init__(self):
        """
        After a dataclass is initialized, this is called to finalize some data
        1. Make sure the original path is an absolute, fully qualified path
        2. If not already set, get the mime type of the file
        3. If the document is from the consume folder, create a shadow copy
           of the file in scratch to work with
        """
        # Always fully qualify the path first thing
        self.original_file = Path(self.original_file).resolve()

        # Get the file type once at init, not when from serialized
        if self.mime_type is None:
            self.mime_type = magic.from_file(self.original_file, mime=True)

    def as_dict(self) -> Dict:
        """
        Serializes the dataclass into a dictionary of only basic types like
        strings and ints
        """
        return {
            "source": int(self.source),
            "original_file": str(self.original_file),
            "mime_type": self.mime_type,
        }

    @staticmethod
    def from_dict(data: Dict) -> "ConsumableDocument":
        """
        Given a serialized dataclass, returns the
        """
        doc = ConsumableDocument(
            DocumentSource(data["source"]),
            Path(data["original_file"]),
            # The mime type is already determined in this case,
            # don't gather a second time
            data["mime_type"],
        )
        return doc

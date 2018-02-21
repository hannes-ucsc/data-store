"""Utilities in this file are used to removed extra fields from index data before adding to the index."""
from jsonschema import validators
from jsonschema import _utils, Draft4Validator
import json
from typing import List
import logging

from dss.util.s3urlcache import S3UrlCache

logger = logging.getLogger(__name__)


# DSS_Draft4Validator = validators.create(meta_schema=_utils.load_schema("draft4"),
#                                         validators={'$ref': _validators.ref,
#                                                     'additionalProperties': _validators.additionalProperties,
#                                                     'properties': _validators.properties_draft4,
#                                                     'required': _validators.required_draft4
#                                                     },
#                                         version="draft4"
#                                         )


def remove_json_fields(json_data: dict, path: List[str], fields: List[str]) -> None:
    """
    Removes fields from the path in json_data.

    :param json_data: The JSON data from which to remove fields.
    :param path: A list of indices (either field names or array indices) forming a path through the JSON data.
    :param fields: A list of fields to remove from the JSON data at the location specified by path.
    """
    current = json_data
    for step in path:
        current = current[step]
    for field in fields:
        current.pop(field)


def scrub_index_data(index_data: dict, bundle_id: str) -> list:
    cache = S3UrlCache()

    def request_json(url):
        return json.loads(cache.resolve(url).decode("utf-8"))

    resolver = validators.RefResolver(referrer='',
                                      base_uri='',
                                      handlers={'http': request_json,
                                                'https': request_json}
                                      )
    extra_fields = []
    extra_documents = []
    for document in index_data.keys():
        core = index_data[document].get('core')
        schema_url = None if core is None else core.get('schema_url')
        if schema_url is not None:
            try:
                schema = request_json(schema_url)
            except Exception as ex:
                extra_documents.append(document)
                logger.warning(f"Unable to retrieve schema from {document} in {bundle_id} "
                               f"because retrieving {schema_url} caused exception: {ex}.")
            else:
                for error in Draft4Validator(schema, resolver=resolver).iter_errors(index_data[document]):
                    if error.validator == 'additionalProperties':
                        path = [document, *error.path]
                        #  Example error message: "Additional properties are not allowed ('extra_lst', 'extra_top' were
                        #  unexpected)" or "'extra', does not match any of the regexes: '^characteristics_.*$'"
                        fields_to_remove = (path, [field for field in _utils.find_additional_properties(error.instance,
                                                                                                        error.schema)])
                    elif error is not None:
                        raise error
                    extra_fields.append(fields_to_remove)
        else:
            logger.warning(f"Unable to retrieve schema_url from {document} in {bundle_id} because "
                           f"core.schema_url does not exist.")
            extra_documents.append(document)
    if extra_documents:
        extra_fields.append(([], extra_documents))
    removed_fields = []
    for path, fields in extra_fields:
        remove_json_fields(index_data, path, fields)
        removed_fields.extend(['.'.join((*path, field)) for field in fields])
    if removed_fields:
        logger.info(f"In {bundle_id}, unexpected additional fields have been removed from the data"
                    f" to be indexed. Removed {removed_fields}.")
    return removed_fields

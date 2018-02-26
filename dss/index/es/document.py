from collections import defaultdict
import ipaddress
import json
import logging
import socket
import typing
import re
from urllib.parse import urlparse
import uuid

from elasticsearch import TransportError
from elasticsearch.helpers import BulkIndexError, bulk, scan
import requests
from requests_http_signature import HTTPSignatureAuth

from dss import Config, DeploymentStage, ESDocType, ESIndexType, Replica
from dss.index.bundle import Bundle, Tombstone
from dss.index.es import ElasticsearchClient, elasticsearch_retry
from dss.index.es.manager import IndexManager
from dss.index.es.validator import scrub_index_data
from dss.storage.identifiers import BundleFQID, ObjectIdentifier

logger = logging.getLogger(__name__)

SCHEMA_URL_PATTERN = r'/(?P<major_version>[0-9]+)\.[0-9]+\.[0-9]+[\w\d/]*/(?P<type>[\w\d]+)(.json)?$'
SCHEMA_URL_REGEX = re.compile(SCHEMA_URL_PATTERN)


class IndexDocument(dict):
    """
    An instance of this class represents a document in an Elasticsearch index.
    """

    def __init__(self, replica: Replica, fqid: ObjectIdentifier, seq=(), **kwargs) -> None:
        super().__init__(seq, **kwargs)
        self.replica = replica
        self.fqid = fqid

    def _write_to_index(self, index_name: str, version: typing.Optional[int] = None):
        """
        Place this document into the given index.

        :param version: if 0, write only if this document is currently absent from the given index
                        if > 0, write only if the specified version of this document is currently present
                        if None, write regardless
        """
        es_client = ElasticsearchClient.get()
        body = self.to_json()
        logger.debug(f"Writing document to index {index_name}: {body}")
        es_client.index(index=index_name,
                        doc_type=ESDocType.doc.name,
                        id=str(self.fqid),
                        body=body,
                        op_type='create' if version == 0 else 'index',
                        version=version if version else None)

    def to_json(self):
        return json.dumps(self)

    def __eq__(self, other: object) -> bool:
        return self is other or (super().__eq__(other) and
                                 isinstance(other, IndexDocument) and  # redundant, but mypy insists
                                 type(self) == type(other) and
                                 self.replica == other.replica and
                                 self.fqid == other.fqid)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(replica={self.replica}, fqid={self.fqid}, {super().__repr__()})"

    @staticmethod
    def _msg(dryrun):
        """
        Returns a unary function that conditionally rewrites a given log message so it makes sense in the context of a
        dry run.

        The message should start with with a verb in -ing form, announcing an action to be taken.
        """

        def msg(s):
            assert s
            assert s[:1].isupper()
            assert s.split(maxsplit=1)[0].endswith('ing')
            return f"Skipped {s[:1].lower() + s[1:]}" if dryrun else s

        return msg


class BundleDocument(IndexDocument):
    """
    An instance of this class represents the Elasticsearch document for a given bundle.
    """

    # Note to implementors, only public methods should have a `dryrun` keyword argument. If they do, the argument
    # should have a default value of False. Protected and private methods may also have a dryrun argument but if they
    # do it must be positional in order to ensure that the argument isn't accidentally dropped along the call chain.

    @classmethod
    def from_bundle(cls, bundle: Bundle):
        self = cls(bundle.replica, bundle.fqid)
        self['manifest'] = bundle.manifest
        self['state'] = 'new'

        # There are two reasons in favor of not using dot in the name of the individual files in the index document,
        # and instead replacing it with an underscore:
        #
        # 1. Ambiguity regarding interpretation/processing of dots in field names, which could potentially change
        #    between Elasticsearch versions. For example, see: https://github.com/elastic/elasticsearch/issues/15951
        #
        # 2. The ES DSL queries are easier to read when there is no ambiguity regarding dot as a field separator.
        #    Therefore, substitute dot for underscore in the key filename portion of the index. As due diligence,
        #    additional investigation should be performed.
        #
        files = {name.replace('.', '_'): content for name, content in bundle.files.items()}
        scrub_index_data(files, str(self.fqid))
        self['files'] = files
        self['uuid'] = self.fqid.uuid
        return self

    @classmethod
    def from_index(cls, replica: Replica, bundle_fqid: BundleFQID, index_name, version=None):
        es_client = ElasticsearchClient.get()
        source = es_client.get(index_name, str(bundle_fqid), ESDocType.doc.name, version=version)['_source']
        return cls(replica, bundle_fqid, source)

    @property
    def files(self):
        return self['files']

    @property
    def manifest(self):
        return self['manifest']

    @elasticsearch_retry(logger)
    def index(self, dryrun=False) -> typing.Tuple[bool, str]:
        """
        Ensure that there is exactly one up-to-date instance of this document in exactly one ES index.

        :param dryrun: if True, only read-only actions will be performed but no ES indices will be modified

        :return: a tuple (modified, index_name) indicating whether an index needed to be updated and what the name of
                 that index is. Note that `modified` may be True even if dryrun is False, indicating that a wet run
                 would have updated the index.
        """
        elasticsearch_retry.add_context(bundle=self)
        index_name = self._prepare_index(dryrun)
        return self._index_into(index_name, dryrun)

    def _index_into(self, index_name: str, dryrun: bool):
        elasticsearch_retry.add_context(index=index_name)
        msg = self._msg(dryrun)
        versions = self._get_indexed_versions()
        old_version = versions.pop(index_name, None)
        if versions:
            logger.warning(msg("Removing stale copies of the bundle document for %s from these index(es): %s."),
                           self.fqid, json.dumps(versions))
            if not dryrun:
                self._remove_versions(versions)
        if old_version:
            assert isinstance(self.fqid, BundleFQID)
            old_doc = self.from_index(self.replica, self.fqid, index_name, version=old_version)
            if self == old_doc:
                logger.info(f"Document for bundle {self.fqid} is already up-to-date "
                            f"in index {index_name} at version {old_version}.")
                return False, index_name
            else:
                logger.warning(msg(f"Updating an older copy of the document for bundle {self.fqid} "
                                   f"in index {index_name} at version {old_version}."))
        else:
            logger.info(msg(f"Writing the document for bundle {self.fqid} "
                            f"to index {index_name} for the first time."))
        if not dryrun:
            self._write_to_index(index_name, version=old_version or 0)
        return True, index_name

    @elasticsearch_retry(logger)
    def entomb(self, tombstone: 'BundleTombstoneDocument', dryrun=False) -> typing.Tuple[bool, str]:
        """
        Ensure that there is exactly one up-to-date instance of a tombstone for this document in exactly one
        ES index. The tombstone data overrides the document's data in the index.

        :param tombstone: The document with which to replace this document in the index.
        :param dryrun: see :py:meth:`~IndexDocument.index`
        :return: see :py:meth:`~IndexDocument.index`
        """
        elasticsearch_retry.add_context(bundle=self, tombstone=tombstone)
        logger.info(f"Writing tombstone for {self.replica.name} bundle: {self.fqid}")
        # Preare the index using the original data such that the tombstone can be placed in the correct index.
        index_name = self._prepare_index(dryrun)
        # Override document with tombstone JSON …
        other = BundleDocument(replica=self.replica, fqid=self.fqid, seq=tombstone)
        # … and place into proper index.
        modified, index_name = other._index_into(index_name, dryrun)
        logger.info(f"Finished writing tombstone for {self.replica.name} bundle: {self.fqid}")
        return modified, index_name

    def _write_to_index(self, index_name: str, version: typing.Optional[int] = None):
        es_client = ElasticsearchClient.get()
        initial_mappings = es_client.indices.get_mapping(index_name)[index_name]['mappings']
        super()._write_to_index(index_name, version=version)
        current_mappings = es_client.indices.get_mapping(index_name)[index_name]['mappings']
        if initial_mappings != current_mappings:
            self._refresh_percolate_queries(index_name)

    def _prepare_index(self, dryrun):
        shape_descriptor = self.get_shape_descriptor()
        index_name = Config.get_es_index_name(ESIndexType.docs, self.replica, shape_descriptor)
        es_client = ElasticsearchClient.get()
        if not dryrun:
            IndexManager.create_index(es_client, self.replica, index_name)
        return index_name

    def get_shape_descriptor(self) -> typing.Optional[str]:
        """
        Return a string identifying the shape/structure/format of the data in this bundle
        document, so that it may be indexed appropriately.

        Currently, this returns a string identifying the metadata schema release major number.
        For example:

            v3 - Bundle contains metadata in the version 3 format
            v4 - Bundle contains metadata in the version 4 format
            ...

        This includes verification that schema major number is the same for all index metadata
        files in the bundle, consistent with the current HCA ingest service behavior. If no
        metadata version information is contained in the bundle, the empty string is returned.
        Currently this occurs in the case of the empty bundle used for deployment testing.

        If/when bundle schemas are available, this function should be updated to reflect the
        bundle schema type and major version number.

        Other projects (non-HCA) may manage their metadata schemas (if any) and schema versions.
        This should be an extension point that is customizable by other projects according to
        their metadata.
        """
        schema_version_map = defaultdict(set)  # type: typing.MutableMapping[str, typing.MutableSet[str]]
        for file_name, file_content in self.files.items():
            schema_url = file_content.get('describedBy')
            core = file_content.get('core')
            if core is not None:
                schema_type = core['type']
                schema_version = core['schema_version']
                schema_version_major = schema_version.split(".")[0]
                schema_version_map[schema_version_major].add(schema_type)
            elif schema_url is not None:
                schema_version_major, schema_type = SCHEMA_URL_REGEX.search(schema_url).group('major_version', 'type')
                schema_version_map[schema_version_major].add(schema_type)
            else:
                logger.info(f"File {file_name} does not contain the 'core' or 'describedBy' section necessary to "
                            f"identify the schema and its version.")
        if schema_version_map:
            schema_versions = schema_version_map.keys()
            assert len(schema_versions) == 1, \
                "The bundle contains mixed schema major version numbers: {}".format(sorted(list(schema_versions)))
            return "v" + list(schema_versions)[0]
        else:
            return None  # No files with schema identifiers were found

    # Alias [foo] has more than one indices associated with it [[bar1, bar2]], can't execute a single index op
    multi_index_error = re.compile(r"Alias \[([^\]]+)\] has more than one indices associated with it "
                                   r"\[\[([^\]]+)\]\], can't execute a single index op")

    def _get_indexed_versions(self) -> typing.MutableMapping[str, int]:
        """
        Returns a dictionary mapping the name of each index containing this document to the
        version of this document in that index. Note that `version` denotes document version, not
        bundle version.
        """
        es_client = ElasticsearchClient.get()
        alias_name = Config.get_es_alias_name(ESIndexType.docs, self.replica)
        # First attempt to get the single instance of the document. The common case is that there is zero or one
        # instance.
        try:
            doc = es_client.get(id=str(self.fqid),
                                index=alias_name,
                                _source=False,
                                stored_fields=[])
            # One instance found
            return {doc['_index']: doc['_version']}
        except TransportError as e:
            if e.status_code == 404:
                # No instance found
                return {}
            elif e.status_code == 400:
                # This could be a general error or an one complaining that we attempted a single-index operation
                # against a multi-index alias. If the latter, we can actually avoid a round trip by parsing the index
                # names out of the error message generated at https://github.com/elastic/elasticsearch/blob/5.5
                # /core/src/main/java/org/elasticsearch/cluster/metadata/IndexNameExpressionResolver.java#L194
                error = e.info.get('error')
                if error:
                    reason = error.get('reason')
                    if reason:
                        match = self.multi_index_error.fullmatch(reason)
                        if match:
                            indices = map(str.strip, match.group(2).split(','))
                            # Now get the document version from all indices in the alias
                            doc = es_client.mget(_source=False,
                                                 stored_fields=[],
                                                 body={
                                                     'docs': [
                                                         {
                                                             '_id': str(self.fqid),
                                                             '_index': index
                                                         } for index in indices
                                                     ]
                                                 })
                            return {doc['_index']: doc['_version'] for doc in doc['docs'] if doc.get('found')}
            raise

    def _remove_versions(self, versions: typing.MutableMapping[str, int]):
        """
        Remove this document from each given index provided that it contains the given version of this document.
        """
        es_client = ElasticsearchClient.get()
        num_ok, errors = bulk(es_client, raise_on_error=False, actions=[{
            '_op_type': 'delete',
            '_index': index_name,
            '_type': ESDocType.doc.name,
            '_version': version,
            '_id': str(self.fqid),
        } for index_name, version in versions.items()])
        for item in errors:
            logger.warning(f"Document deletion failed: {json.dumps(item)}")

    def _refresh_percolate_queries(self, index_name: str):
        # When dynamic templates are used and queries for percolation have been added
        # to an index before the index contains mappings of fields referenced by those queries,
        # the queries must be reloaded when the mappings are present for the queries to match.
        # See: https://github.com/elastic/elasticsearch/issues/5750
        subscription_index_name = Config.get_es_index_name(ESIndexType.subscriptions, self.replica)
        es_client = ElasticsearchClient.get()
        if not es_client.indices.exists(subscription_index_name):
            return
        subscription_queries = [{'_index': index_name,
                                 '_type': ESDocType.query.name,
                                 '_id': hit['_id'],
                                 '_source': hit['_source']['es_query']
                                 }
                                for hit in scan(es_client,
                                                index=subscription_index_name,
                                                doc_type=ESDocType.subscription.name,
                                                query={'query': {'match_all': {}}})
                                ]

        if subscription_queries:
            try:
                bulk(es_client, iter(subscription_queries), refresh=True)
            except BulkIndexError as ex:
                logger.error(f"Error occurred when adding subscription queries "
                             f"to index {index_name} Errors: {ex.errors}")

    def notify(self, index_name):
        subscription_ids = self._find_matching_subscriptions(index_name)
        self._notify_subscribers(subscription_ids)

    def _find_matching_subscriptions(self, index_name: str) -> typing.MutableSet[str]:
        percolate_document = {
            'query': {
                'percolate': {
                    'field': "query",
                    'document_type': ESDocType.doc.name,
                    'document': self
                }
            }
        }
        subscription_ids = set()
        for hit in scan(ElasticsearchClient.get(),
                        index=index_name,
                        query=percolate_document):
            subscription_ids.add(hit["_id"])
        logger.debug(f"Found {len(subscription_ids)} matching subscription(s).")
        return subscription_ids

    def _notify_subscribers(self, subscription_ids: typing.MutableSet[str]):
        for subscription_id in subscription_ids:
            try:
                # TODO Batch this request
                subscription = self._get_subscription(subscription_id)
                self._notify_subscriber(subscription)
            except Exception:
                logger.error(f"Error occurred while processing subscription {subscription_id} "
                             f"for bundle {self.fqid}.", exc_info=True)

    def _get_subscription(self, subscription_id: str) -> dict:
        subscription_query = {
            'query': {
                'ids': {
                    'type': ESDocType.subscription.name,
                    'values': [subscription_id]
                }
            }
        }
        response = ElasticsearchClient.get().search(
            index=Config.get_es_index_name(ESIndexType.subscriptions, self.replica),
            body=subscription_query)
        hits = response['hits']['hits']
        assert len(hits) == 1
        hit = hits[0]
        assert hit['_id'] == subscription_id
        subscription = hit['_source']
        assert 'id' not in subscription
        subscription['id'] = subscription_id
        return subscription

    def _notify_subscriber(self, subscription: dict):
        subscription_id = subscription['id']
        transaction_id = str(uuid.uuid4())
        payload = {
            "transaction_id": transaction_id,
            "subscription_id": subscription_id,
            "es_query": subscription['es_query'],
            "match": {
                "bundle_uuid": self.fqid.uuid,
                "bundle_version": self.fqid.version,
            }
        }
        callback_url = subscription['callback_url']

        # FIXME wrap all errors in this block with an exception handler
        if DeploymentStage.IS_PROD():
            allowed_schemes = {'https'}
        else:
            allowed_schemes = {'https', 'http'}

        assert urlparse(callback_url).scheme in allowed_schemes, "Unexpected scheme for callback URL"

        if DeploymentStage.IS_PROD():
            hostname = urlparse(callback_url).hostname
            for family, socktype, proto, canonname, sockaddr in socket.getaddrinfo(hostname, port=None):
                msg = "Callback hostname resolves to forbidden network"
                assert ipaddress.ip_address(sockaddr[0]).is_global, msg  # type: ignore

        auth = None
        if "hmac_secret_key" in subscription:
            auth = HTTPSignatureAuth(key=subscription['hmac_secret_key'].encode(),
                                     key_id=subscription.get("hmac_key_id", "hca-dss:" + subscription_id))
        response = requests.post(callback_url, json=payload, auth=auth)

        # TODO (mbaumann) Add webhook retry logic
        if 200 <= response.status_code < 300:
            logger.info(f"Successfully notified for subscription {subscription_id}"
                        f" for bundle {self.fqid} with transaction id {transaction_id} "
                        f"Code: {response.status_code}")
        else:
            logger.warning(f"Failed notification for subscription {subscription_id}"
                           f" for bundle {self.fqid} with transaction id {transaction_id} "
                           f"Code: {response.status_code}")


class BundleTombstoneDocument(IndexDocument):
    """
    The index document representing a bundle tombstone.
    """

    @classmethod
    def from_tombstone(cls, tombstone: Tombstone) -> 'BundleTombstoneDocument':
        self = cls(tombstone.replica, tombstone.fqid, tombstone.body)
        self['uuid'] = self.fqid.uuid
        return self

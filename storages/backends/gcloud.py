import mimetypes
from datetime import timedelta
from tempfile import SpooledTemporaryFile
from gzip import GzipFile

from django.core.exceptions import ImproperlyConfigured, SuspiciousOperation
from django.core.files.base import File
from django.core.files.storage import Storage
from django.utils import timezone
from django.utils.deconstruct import deconstructible
from django.utils.encoding import force_bytes, smart_str

from storages.utils import (
    check_location, clean_name, get_available_overwrite_name, safe_join,
    setting,
)

try:
    from google.cloud.storage import Blob, Client
    from google.cloud.exceptions import Conflict, NotFound
except ImportError:
    raise ImproperlyConfigured("Could not load Google Cloud Storage bindings.\n"
                               "See https://github.com/GoogleCloudPlatform/gcloud-python")


class GoogleCloudFile(File):
    def __init__(self, name, mode, storage):
        self.name = name
        self.mime_type = mimetypes.guess_type(name)[0]
        self._mode = mode
        self._storage = storage
        self.blob = storage.bucket.get_blob(name)
        if not self.blob and 'w' in mode:
            self.blob = Blob(
                self.name, storage.bucket,
                chunk_size=setting('GS_BLOB_CHUNK_SIZE'))
        self._file = None
        self._is_dirty = False

    @property
    def size(self):
        return self.blob.size

    def _get_file(self):
        if self._file is None:
            self._file = SpooledTemporaryFile(
                max_size=self._storage.max_memory_size,
                suffix=".GSStorageFile",
                dir=setting("FILE_UPLOAD_TEMP_DIR")
            )
            if 'r' in self._mode:
                self._is_dirty = False
                self.blob.download_to_file(self._file)
                self._file.seek(0)
            if self._storage.gzip and self.blob.content_encoding == 'gzip':
                self._file = GzipFile(mode=self._mode, fileobj=self._file, mtime=0.0)
        return self._file

    def _set_file(self, value):
        self._file = value

    file = property(_get_file, _set_file)

    def read(self, num_bytes=None):
        if 'r' not in self._mode:
            raise AttributeError("File was not opened in read mode.")

        if num_bytes is None:
            num_bytes = -1

        return super(GoogleCloudFile, self).read(num_bytes)

    def write(self, content):
        if 'w' not in self._mode:
            raise AttributeError("File was not opened in write mode.")
        self._is_dirty = True
        return super(GoogleCloudFile, self).write(force_bytes(content))

    def close(self):
        if self._file is not None:
            if self._is_dirty:
                self.blob.upload_from_file(
                    self.file, rewind=True, content_type=self.mime_type,
                    predefined_acl=self._storage.default_acl)
            self._file.close()
            self._file = None


@deconstructible
class GoogleCloudStorage(Storage):
    project_id = setting('GS_PROJECT_ID')
    credentials = setting('GS_CREDENTIALS')
    bucket_name = setting('GS_BUCKET_NAME')
    location = setting('GS_LOCATION', '')
    auto_create_bucket = setting('GS_AUTO_CREATE_BUCKET', False)
    auto_create_acl = setting('GS_AUTO_CREATE_ACL', 'projectPrivate')
    default_acl = setting('GS_DEFAULT_ACL')

    expiration = setting('GS_EXPIRATION', timedelta(seconds=86400))
    gzip = setting('GS_IS_GZIPPED', False)

    file_name_charset = setting('GS_FILE_NAME_CHARSET', 'utf-8')
    file_overwrite = setting('GS_FILE_OVERWRITE', True)
    cache_control = setting('GS_CACHE_CONTROL')
    # The max amount of memory a returned file can take up before being
    # rolled over into a temporary file on disk. Default is 0: Do not roll over.
    max_memory_size = setting('GS_MAX_MEMORY_SIZE', 0)

    def __init__(self, **settings):
        # check if some of the settings we've provided as class attributes
        # need to be overwritten with values passed in here
        for name, value in settings.items():
            if hasattr(self, name):
                setattr(self, name, value)

        check_location(self)

        self._bucket = None
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = Client(
                project=self.project_id,
                credentials=self.credentials
            )
        return self._client

    @property
    def bucket(self):
        if self._bucket is None:
            self._bucket = self._get_or_create_bucket(self.bucket_name)
        return self._bucket

    def _get_or_create_bucket(self, name):
        """
        Returns bucket. If auto_create_bucket is True, creates bucket if it
        doesn't exist.
        """
        bucket = self.client.bucket(name)
        if self.auto_create_bucket:
            try:
                new_bucket = self.client.create_bucket(name)
                new_bucket.acl.save_predefined(self.auto_create_acl)
                return new_bucket
            except Conflict:
                # Bucket already exists
                pass
        return bucket

    def _normalize_name(self, name):
        """
        Normalizes the name so that paths like /path/to/ignored/../something.txt
        and ./file.txt work.  Note that clean_name adds ./ to some paths so
        they need to be fixed here. We check to make sure that the path pointed
        to is not outside the directory specified by the LOCATION setting.
        """
        try:
            return safe_join(self.location, name)
        except ValueError:
            raise SuspiciousOperation("Attempted access to '%s' denied." %
                                      name)

    def _encode_name(self, name):
        return smart_str(name, encoding=self.file_name_charset)

    def _compress_content(self, content):
        """Gzip a given string content."""
        content.seek(0)
        zbuf = io.BytesIO()
        #  The GZIP header has a modification time attribute (see http://www.zlib.org/rfc-gzip.html)
        #  This means each time a file is compressed it changes even if the other contents don't change
        #  For Google Storage this defeats detection of changes using MD5 sums on gzipped files
        #  Fixing the mtime at 0.0 at compression time avoids this problem
        zfile = GzipFile(mode='wb', fileobj=zbuf, mtime=0.0)
        try:
            zfile.write(force_bytes(content.read()))
        finally:
            zfile.close()
        zbuf.seek(0)
        return zbuf

    def _open(self, name, mode='rb'):
        name = self._normalize_name(clean_name(name))
        file_object = GoogleCloudFile(name, mode, self)
        if not file_object.blob:
            raise IOError(u'File does not exist: %s' % name)
        return file_object

    def _save(self, name, content):
        cleaned_name = clean_name(name)
        name = self._normalize_name(cleaned_name)

        content.name = cleaned_name
        encoded_name = self._encode_name(name)
        file = GoogleCloudFile(encoded_name, 'rw', self)
        file.blob.cache_control = self.cache_control
        if self.gzip:
            content = self._compress_content(content)
            file.blob.content_encoding = 'gzip'
        file.blob.upload_from_file(
            content, rewind=True, size=content.size,
            content_type=file.mime_type, predefined_acl=self.default_acl)
        return cleaned_name

    def delete(self, name):
        name = self._normalize_name(clean_name(name))
        self.bucket.delete_blob(self._encode_name(name))

    def exists(self, name):
        if not name:  # root element aka the bucket
            try:
                self.client.get_bucket(self.bucket)
                return True
            except NotFound:
                return False

        name = self._normalize_name(clean_name(name))
        return bool(self.bucket.get_blob(self._encode_name(name)))

    def listdir(self, name):
        name = self._normalize_name(clean_name(name))
        # For bucket.list_blobs and logic below name needs to end in /
        # but for the root path "" we leave it as an empty string
        if name and not name.endswith('/'):
            name += '/'

        iterator = self.bucket.list_blobs(prefix=self._encode_name(name), delimiter='/')
        blobs = list(iterator)
        prefixes = iterator.prefixes

        files = []
        dirs = []

        for blob in blobs:
            parts = blob.name.split("/")
            files.append(parts[-1])
        for folder_path in prefixes:
            parts = folder_path.split("/")
            dirs.append(parts[-2])

        return list(dirs), files

    def _get_blob(self, name):
        # Wrap google.cloud.storage's blob to raise if the file doesn't exist
        blob = self.bucket.get_blob(name)

        if blob is None:
            raise NotFound(u'File does not exist: {}'.format(name))

        return blob

    def size(self, name):
        name = self._normalize_name(clean_name(name))
        blob = self._get_blob(self._encode_name(name))
        return blob.size

    def modified_time(self, name):
        name = self._normalize_name(clean_name(name))
        blob = self._get_blob(self._encode_name(name))
        return timezone.make_naive(blob.updated)

    def get_modified_time(self, name):
        name = self._normalize_name(clean_name(name))
        blob = self._get_blob(self._encode_name(name))
        updated = blob.updated
        return updated if setting('USE_TZ') else timezone.make_naive(updated)

    def get_created_time(self, name):
        """
        Return the creation time (as a datetime) of the file specified by name.
        The datetime will be timezone-aware if USE_TZ=True.
        """
        name = self._normalize_name(clean_name(name))
        blob = self._get_blob(self._encode_name(name))
        created = blob.time_created
        return created if setting('USE_TZ') else timezone.make_naive(created)

    def url(self, name):
        """
        Return public url or a signed url for the Blob.
        This DOES NOT check for existance of Blob - that makes codes too slow
        for many use cases.
        """
        name = self._normalize_name(clean_name(name))
        blob = self.bucket.blob(self._encode_name(name))

        if self.default_acl == 'publicRead':
            return blob.public_url
        return blob.generate_signed_url(self.expiration)

    def get_available_name(self, name, max_length=None):
        name = clean_name(name)
        if self.file_overwrite:
            return get_available_overwrite_name(name, max_length)
        return super(GoogleCloudStorage, self).get_available_name(name, max_length)

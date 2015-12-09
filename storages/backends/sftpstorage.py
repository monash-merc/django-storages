from __future__ import print_function
# SFTP storage backend for Django.
# Author: Brent Tubbs <brent.tubbs@gmail.com>
# License: MIT
#
# Modeled on the FTP storage by Rafal Jonca <jonca.rafal@gmail.com>
#
# Settings:
#
# SFTP_STORAGE_HOST - The hostname where you want the files to be saved.
#
# SFTP_STORAGE_ROOT - The root directory on the remote host into which files
# should be placed.  Should work the same way that STATIC_ROOT works for local
# files.  Must include a trailing slash.
#
# SFTP_STORAGE_PARAMS (Optional) - A dictionary containing connection
# parameters to be passed as keyword arguments to
# paramiko.SSHClient().connect() (do not include hostname here).  See
# http://www.lag.net/paramiko/docs/paramiko.SSHClient-class.html#connect for
# details
#
# SFTP_STORAGE_INTERACTIVE (Optional) - A boolean indicating whether to prompt
# for a password if the connection cannot be made using keys, and there is not
# already a password in SFTP_STORAGE_PARAMS.  You can set this to True to
# enable interactive login when running 'manage.py collectstatic', for example.
#
#   DO NOT set SFTP_STORAGE_INTERACTIVE to True if you are using this storage
#   for files being uploaded to your site by users, because you'll have no way
#   to enter the password when they submit the form..
#
# SFTP_STORAGE_FILE_MODE (Optional) - A bitmask for setting permissions on
# newly-created files.  See http://docs.python.org/library/os.html#os.chmod for
# acceptable values.
#
# SFTP_STORAGE_DIR_MODE (Optional) - A bitmask for setting permissions on
# newly-created directories.  See
# http://docs.python.org/library/os.html#os.chmod for acceptable values.
#
#   Hint: if you start the mode number with a 0 you can express it in octal
#   just like you would when doing "chmod 775 myfile" from bash.
#
# SFTP_STORAGE_UID (Optional) - uid of the account that should be set as owner
# of the files on the remote host.  You have to be root to set this.
#
# SFTP_STORAGE_GID (Optional) - gid of the group that should be set on the
# files on the remote host.  You have to be a member of the group to set this.
# SFTP_KNOWN_HOST_FILE (Optional) - absolute path of know host file, if it isn't
# set "~/.ssh/known_hosts" will be used


import getpass
import os
import paramiko
import posixpath
import stat
from datetime import datetime

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import File

from storages.compat import urlparse, BytesIO, Storage


class SFTPStorage(Storage):
    """
    http://docs.paramiko.org/en/1.16/api/client.html#paramiko.client.SSHClient.connect
    """
    host = getattr(settings, 'SFTP_STORAGE_HOST', None)

    # if present, settings.SFTP_STORAGE_PARAMS should be a dict with params
    # matching the keyword arguments to paramiko.SSHClient().connect().  So
    # you can put username/password there.  Or you can omit all that if
    # you're using keys.
    params = getattr(settings, 'SFTP_STORAGE_PARAMS', {})

    interactive = getattr(settings, 'SFTP_STORAGE_INTERACTIVE', False)
    file_mode = getattr(settings, 'SFTP_STORAGE_FILE_MODE', None)
    dir_mode = getattr(settings, 'SFTP_STORAGE_DIR_MODE', None)
    uid = getattr(settings, 'SFTP_STORAGE_UID', None)
    gid = getattr(settings, 'SFTP_STORAGE_GID', None)
    known_host_file = getattr(settings, 'SFTP_KNOWN_HOST_FILE', None)
    root_path = getattr(settings, 'SFTP_STORAGE_ROOT', None)
    base_url = settings.MEDIA_URL

    # for now it's all posix paths.  Maybe someday we'll support figuring
    # out if the remote host is windows.
    _pathmod = posixpath

    def __init__(self, **settings):
        """

        :param kwargs:
        :return:
        """
        for name, value in settings.items():
            if hasattr(self, name):
                setattr(self, name, value)
            else:
                self.params[name] = value

        if self.host is None:
            raise ImproperlyConfigured('host setting missing for SFTP storage')

        if self.root_path is None:
            raise ImproperlyConfigured(
                'root_path setting missing for SFTP storage')

    def _connect(self):
        self._ssh = paramiko.SSHClient()

        if self.known_host_file is not None:
            self._ssh.load_host_keys(self.known_host_file)
        else:
            # automatically add host keys from current user.
            self._ssh.load_host_keys(os.path.expanduser(os.path.join("~", ".ssh", "known_hosts")))

        # and automatically add new host keys for hosts we haven't seen before.
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            self._ssh.connect(self.host, **self.params)
        except paramiko.AuthenticationException as e:
            if self.interactive and 'password' not in self.params:
                # If authentication has failed, and we haven't already tried
                # username/password, and configuration allows it, then try
                # again with username/password.
                if 'username' not in self.params:
                    self.params['username'] = getpass.getuser()
                self.params['password'] = getpass.getpass()
                self._connect()
            else:
                raise paramiko.AuthenticationException(e)
        except Exception as e:
            print(e)

        if not hasattr(self, '_sftp'):
            self._sftp = self._ssh.open_sftp()

    @property
    def sftp(self):
        """Lazy SFTP connection"""
        if not hasattr(self, '_sftp'):
            self._connect()
        return self._sftp

    def _join(self, *args):
        # Use the path module for the remote host type to join a path together
        return self._pathmod.join(*args)

    def _remote_path(self, name):
        return self._join(self.root_path, name)

    def _open(self, name, mode='rb'):
        return SFTPStorageFile(name, self, mode)

    def _read(self, name):
        remote_path = self._remote_path(name)
        return self.sftp.open(remote_path, 'rb')

    def _chown(self, path, uid=None, gid=None):
        """Set uid and/or gid for file at path."""
        # Paramiko's chown requires both uid and gid, so look them up first if
        # we're only supposed to set one.
        if uid is None or gid is None:
            attr = self.sftp.stat(path)
            uid = uid or attr.st_uid
            gid = gid or attr.st_gid
        self.sftp.chown(path, uid, gid)

    def _mkdir(self, path):
        """Create directory, recursing up to create parent dirs if
        necessary."""
        parent = self._pathmod.dirname(path)
        if not self.exists(parent):
            self._mkdir(parent)
        self.sftp.mkdir(path)

        if self.dir_mode is not None:
            self.sftp.chmod(path, self.dir_mode)

        if self.uid or self.gid:
            self._chown(path, uid=self.uid, gid=self.gid)

    def _save(self, name, content):
        """Save file via SFTP."""
        content.open()
        path = self._remote_path(name)
        dirname = self._pathmod.dirname(path)
        if not self.exists(dirname):
            self._mkdir(dirname)

        f = self.sftp.open(path, 'wb')
        f.write(content.file.read())
        f.close()

        # set file permissions if configured
        if self.file_mode is not None:
            self.sftp.chmod(path, self.file_mode)
        if self.uid or self.gid:
            self._chown(path, uid=self.uid, gid=self.gid)
        return name

    def delete(self, name):
        remote_path = self._remote_path(name)
        self.sftp.remove(remote_path)

    def exists(self, name):
        # Try to retrieve file info.  Return true on success, false on failure.
        remote_path = self._remote_path(name)
        try:
            self.sftp.stat(remote_path)
            return True
        except IOError:
            return False

    def _isdir_attr(self, item):
        # Return whether an item in sftp.listdir_attr results is a directory
        if item.st_mode is not None:
            return stat.S_IFMT(item.st_mode) == stat.S_IFDIR
        else:
            return False

    def listdir(self, path):
        remote_path = self._remote_path(path)
        dirs, files = [], []
        for item in self.sftp.listdir_attr(remote_path):
            if self._isdir_attr(item):
                dirs.append(item.filename)
            else:
                files.append(item.filename)
        return dirs, files

    def size(self, name):
        remote_path = self._remote_path(name)
        return self.sftp.stat(remote_path).st_size

    def accessed_time(self, name):
        remote_path = self._remote_path(name)
        utime = self.sftp.stat(remote_path).st_atime
        return datetime.fromtimestamp(utime)

    def modified_time(self, name):
        remote_path = self._remote_path(name)
        utime = self.sftp.stat(remote_path).st_mtime
        return datetime.fromtimestamp(utime)

    def url(self, name):
        if self.base_url is None:
            raise ValueError("This file is not accessible via a URL.")
        return urlparse.urljoin(self.base_url, name).replace('\\', '/')


class SFTPStorageFile(File):
    def __init__(self, name, storage, mode):
        self._name = name
        self._storage = storage
        self._mode = mode
        self._is_dirty = False
        self.file = BytesIO()
        self._is_read = False

    @property
    def size(self):
        if not hasattr(self, '_size'):
            self._size = self._storage.size(self._name)
        return self._size

    def read(self, num_bytes=None):
        if not self._is_read:
            self.file = self._storage._read(self._name)
            self._is_read = True

        return self.file.read(num_bytes)

    def write(self, content):
        if 'w' not in self._mode:
            raise AttributeError("File was opened for read-only access.")
        self.file = BytesIO(content)
        self._is_dirty = True
        self._is_read = True

    def close(self):
        if self._is_dirty:
            self._storage._save(self._name, self.file.getvalue())
        self.file.close()

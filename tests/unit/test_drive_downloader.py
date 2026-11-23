import base64
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.workspace.drive_downloader import download_file, verify_file_version
from app.workspace.drive_storage import DriveLink
from app.workspace.service import WorkspaceError


@pytest.mark.parametrize(
    ('mime', 'method', 'parser_mime'),
    [
        ('application/pdf', 'files.get', 'application/pdf'),
        ('application/vnd.google-apps.document', 'files.export',
         'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
        ('application/vnd.google-apps.spreadsheet', 'files.export',
         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
        ('application/vnd.google-apps.presentation', 'files.export',
         'application/vnd.openxmlformats-officedocument.presentationml.presentation'),
    ],
)
def test_download_versioned_bytes_over_rest(mime, method, parser_mime):
    service = Mock()
    service.google.get_user_google_connection.return_value = SimpleNamespace(
        status='connected', google_sub='sub', link_version=2,
    )
    service.read.side_effect = [
        {'data': {'id': 'abc', 'name': 'Document', 'mimeType': mime, 'version': '4',
                  'capabilities': {'canDownload': True}}},
        {'data': {'content_base64': base64.b64encode(b'file').decode()}},
    ]
    result = download_file(service, DriveLink(1, 'sub', 2), 'abc', expected_version='4')
    assert result.file.version == '4'
    assert result.parser_mime == parser_mime
    assert result.content == b'file'
    args = service.read.call_args
    assert args.kwargs == {'force_rest': True}
    assert args.args[2]['method'] == method
    parameters = args.args[2]['parameters']
    assert parameters.get('mimeType', parser_mime) == parser_mime
    if method == 'files.get':
        assert parameters['alt'] == 'media'


@pytest.mark.parametrize('code', ['PERMISSION_DENIED', 'NOT_FOUND'])
def test_download_preserves_provider_permission_and_not_found_errors(code):
    service = Mock()
    service.google.get_user_google_connection.return_value = SimpleNamespace(
        status='connected', google_sub='sub', link_version=2,
    )
    service.read.side_effect = WorkspaceError(code, 'provider response')
    with pytest.raises(WorkspaceError) as exc:
        download_file(service, DriveLink(1, 'sub', 2), 'abc')
    assert exc.value.code == code


def test_verify_file_version_rejects_new_source_version():
    service = Mock()
    service.google.get_user_google_connection.return_value = SimpleNamespace(
        status='connected', google_sub='sub', link_version=2,
    )
    service.read.return_value = {'data': {
        'id': 'abc', 'name': 'Document', 'mimeType': 'application/pdf', 'version': '5',
        'capabilities': {'canDownload': True},
    }}
    with pytest.raises(WorkspaceError) as exc:
        verify_file_version(service, DriveLink(1, 'sub', 2), 'abc', '4')
    assert exc.value.code == 'FILE_VERSION_CHANGED'

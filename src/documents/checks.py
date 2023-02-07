from django.core.checks import Error
from django.core.checks import register
from documents.signals import document_consumer_declaration


@register()
def parser_check(app_configs, **kwargs):

    parsers = []
    for response in document_consumer_declaration.send(None):
        parsers.append(response[1])

    if len(parsers) == 0:
        return [
            Error(
                "No parsers found. This is a bug. The consumer won't be "
                "able to consume any documents without parsers.",
            ),
        ]
    else:
        return []

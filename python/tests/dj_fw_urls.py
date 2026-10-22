"""URLconf for tests/test_fw_django.py."""
from django.http import Http404, HttpResponse
from django.template.response import TemplateResponse
from django.shortcuts import render
from django.urls import include, path, re_path

import insight_sdk


def item(request, pk):
    insight_sdk.set_tag("view", "item")
    raise ValueError("django item boom %d" % pk)


def missing(request):
    raise Http404("nothing here")


def ok(request):
    return HttpResponse("ok")


def slow(request, n):
    import time

    insight_sdk.set_tag("n", str(n))
    time.sleep(0.15)
    raise RuntimeError("slow boom %s" % n)


async def aitem(request, pk):
    insight_sdk.set_tag("async_view", "1")
    raise ValueError("django async boom %d" % pk)


class Explosive:
    @property
    def value(self):
        raise ZeroDivisionError("template property boom")


def tmpl_response(request):
    return TemplateResponse(request, "explode.html", {"obj": Explosive()})


def tmpl_render(request):
    return render(request, "explode.html", {"obj": Explosive()})


def tmpl_syntax(request):
    return render(request, "syntax.html", {})


def regex_item(request, pk):
    raise ValueError("regex boom")


try:
    from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
    from rest_framework.views import APIView
    from rest_framework.response import Response

    class Down(APIException):
        status_code = 503
        default_detail = "backend down"

    class DrfBoom(APIView):
        def get(self, request, pk):
            raise ValueError("drf boom %s" % pk)

    class DrfDown(APIView):
        def get(self, request):
            raise Down()

    class Drf404(APIView):
        def get(self, request):
            raise NotFound("no such thing")

    class Drf400(APIView):
        def get(self, request):
            raise ValidationError({"field": ["bad"]})

    class Drf403(APIView):
        def get(self, request):
            raise PermissionDenied()

    class DrfDjango404(APIView):
        def get(self, request):
            raise Http404()

    class DrfOk(APIView):
        def get(self, request):
            return Response({"ok": True})

    api_patterns = [
        path("boom/<int:pk>/", DrfBoom.as_view()),
        path("down/", DrfDown.as_view()),
        path("notfound/", Drf404.as_view()),
        path("invalid/", Drf400.as_view()),
        path("forbidden/", Drf403.as_view()),
        path("dj404/", DrfDjango404.as_view()),
        path("ok/", DrfOk.as_view()),
    ]
except ImportError:  # pragma: no cover
    api_patterns = []

urlpatterns = [
    path("items/<int:pk>/", item),
    path("missing/", missing),
    path("ok/", ok),
    path("slow/<int:n>/", slow),
    path("aitems/<int:pk>/", aitem),
    path("tmpl-response/", tmpl_response),
    path("tmpl-render/", tmpl_render),
    path("tmpl-syntax/", tmpl_syntax),
    re_path(r"^legacy/(?P<pk>\d+)/$", regex_item),
    path("api/", include(api_patterns)),
]

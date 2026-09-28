from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def page_url(context, number):
    query = context["request"].GET.copy()
    query["page"] = number
    return "?" + query.urlencode()
